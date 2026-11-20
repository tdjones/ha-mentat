"""The two wire dialects behind one interface.

Everything above this module calls DecisionClient.ask() and handles jevclient's
exceptions, so the coordinator, the actions and the AI Task entity never learn
which dialect answered. DecisionClient.body_size() measures the body a dialect
would post, so the budget check estimates from the bytes that will be sent.
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import json
import math
import re
import time
from collections.abc import Mapping
from typing import Any, Final, Protocol

import aiohttp
from homeassistant.helpers.json import json_bytes
from jevclient import (
    Box,
    JevAuthError,
    JevClient,
    JevConnectionError,
    JevError,
    JevOverloadedError,
    JevRateLimitError,
    JevResponse,
    JevResponseError,
    JevValidationError,
    Question,
    Usage,
)
from jevclient.models import parse_answer
from yarl import URL

from .const import MAX_IMAGES, MAX_REPLY_BYTES, REQUEST_TIMEOUT_SECONDS, SYSTEMONE_PATH
from .pictures import MIME_TYPE, Picture, Pictures
from .providers import Dialect, ImageFormat, Provider, resolve_url

type StateType = str | Mapping[str, Any] | list[Any]

# jevclient's message for a 200 reply with no top-level answers object. A Clef
# server's envelope reads exactly like that to the TypeSafe client, so it is how a
# wrong dialect shows itself. tests/test_client.py drives the real JevClient into
# it, so a change of wording in the library fails there.
_NO_ANSWERS: Final = "the reply carries no answers object"

# The body keys this module writes. `extra` cannot replace them.
_RESERVED: Final = frozenset({"model", "state", "questions", "images"})

# What a model id may hold where it fills {model} in a path: RFC 3986's
# unreserved characters. A `/`, a `%` or a `?` would let it name another
# endpoint, which yarl reaches by folding `..` away before the key is sent.
_PATH_SEGMENT: Final = re.compile(r"[A-Za-z0-9._~-]+")

# Workers AI error codes (developers.cloudflare.com/workers-ai/platform/errors,
# read 2026-10-30) and the Cloudflare API's own authentication codes.
_CF_AUTH: Final = frozenset({9103, 9106, 9109, 10000, 10001})
_CF_FORBIDDEN: Final = frozenset({3023, 3041, 5016, 5018})
_CF_INVALID: Final = frozenset({3003, 3006, 3039, 3042, 5004, 5005, 5006, 5007})
_CF_TIMEOUT: Final = frozenset({3007, 3008})
_CF_OVERLOADED: Final = frozenset({3040})
# 3036 is the free daily allocation spent, 5035 a model that needs the paid plan.
# Both are "add credit", which status 402 means everywhere else.
_CF_NO_CREDIT: Final = frozenset({3036, 5035})


class WrongDialectError(JevResponseError):
    """The server answered, but not in the shape this dialect reads."""


class DecisionClient(Protocol):
    """One endpoint, one key, one dialect."""

    @property
    def dialect(self) -> Dialect: ...

    async def ask(
        self,
        state: StateType,
        questions: Mapping[str, Question],
        *,
        model: str | None = None,
        pictures: Pictures | None = None,
    ) -> JevResponse: ...

    def body_size(
        self, state: StateType, questions: Mapping[str, Question], *, model: str | None = None
    ) -> int:
        """Bytes of the JSON body ask() would post, pictures left out.

        A picture is billed by its pixels (providers.image_tokens). Its base64
        read through the bytes-per-token ratio would count it many times over.
        """
        ...

    async def async_close(self) -> None: ...


def _check_image_count(count: int) -> None:
    if count > MAX_IMAGES:
        raise JevValidationError(
            f"a request carries at most {MAX_IMAGES} images, got {count}; nothing was sent"
        )


class _JevClient(JevClient):
    """jevclient with a usage block it cannot misread.

    jevclient adds up `int(usage[...])` itself, so a null or a word there would
    escape as a TypeError or a ValueError rather than a JevError, and a
    ValueError reads as a request refused before sending.
    """

    @staticmethod
    def _parse(data: Any, latency_ms: float) -> JevResponse:
        if isinstance(data, Mapping):
            usage = data.get("usage")
            if not isinstance(usage, Mapping):
                usage = {}
            data = {
                **data,
                "usage": {
                    "input_tokens": _token_count(usage, "input_tokens", "prompt_tokens"),
                    "output_tokens": _token_count(usage, "output_tokens", "completion_tokens"),
                },
            }
        return JevClient._parse(data, latency_ms)


class TypeSafeClient:
    """The published API, through jevclient."""

    def __init__(
        self,
        api_key: str,
        *,
        session: aiohttp.ClientSession,
        base_url: str,
        model: str,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._model = model
        self._client = _JevClient(
            api_key, session=session, base_url=base_url, model=model, timeout=timeout
        )

    @property
    def dialect(self) -> Dialect:
        return Dialect.TYPESAFE

    async def ask(
        self,
        state: StateType,
        questions: Mapping[str, Question],
        *,
        model: str | None = None,
        pictures: Pictures | None = None,
    ) -> JevResponse:
        assets = pictures.assets if pictures else {}
        _check_image_count(len(assets))
        try:
            response = await self._client.ask(state, questions, model=model, assets=assets or None)
        except ValueError as err:
            # jevclient checks a few things before sending and raises ValueError.
            # Callers handle JevError only.
            raise JevValidationError(f"{err}; nothing was sent") from err
        except JevResponseError as err:
            if err.status is None and str(err) == _NO_ANSWERS:
                raise WrongDialectError(
                    "the server answered without an answers object, which is how a "
                    "Clef server's reply reads to the TypeSafe dialect"
                ) from err
            raise
        return _finite(response)

    def body_size(
        self, state: StateType, questions: Mapping[str, Question], *, model: str | None = None
    ) -> int:
        # jevclient posts json= through Home Assistant's session, whose encoder
        # is json_bytes: compact UTF-8, so an accented letter is its two bytes.
        wire = {
            "state": state,
            "model": model or self._model,
            "questions": {key: question.as_payload() for key, question in questions.items()},
        }
        return len(json_bytes(wire))

    async def async_close(self) -> None:
        await self._client.async_close()


class ClefClient:
    """The Clef dialect: Cloudflare Workers AI, clef_mlx.py and alike.

    The question payloads are jevclient's own, and so are the answers and the
    exceptions, so nothing above this class sees a difference. What differs is the
    address (a {model} template or base + /v1/systemone), images sent as an ordered
    list, the {success, result, errors} envelope, and that a Box question does not
    exist here.
    """

    def __init__(
        self,
        api_key: str,
        *,
        session: aiohttp.ClientSession,
        url: str,
        model: str,
        image_format: ImageFormat = ImageFormat.DATA_URL,
        extra: Mapping[str, Any] | None = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._api_key = api_key
        self._session = session
        self._url = url.rstrip("/")
        self._model = model
        # Clef reads images inline; assets are a TypeSafe idea.
        self._image_format = (
            ImageFormat.DATA_URL if image_format is ImageFormat.ASSETS else image_format
        )
        self._extra = {k: v for k, v in (extra or {}).items() if k not in _RESERVED}
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    @property
    def dialect(self) -> Dialect:
        return Dialect.CLEF

    def endpoint(self, model: str) -> str:
        """The URL one request goes to, or JevValidationError for a model that
        would not stay one segment of its path.
        """
        if "{model}" not in self._url:
            return f"{self._url}{SYSTEMONE_PATH}"
        if not _PATH_SEGMENT.fullmatch(model) or not model.strip("."):
            raise JevValidationError(
                f"model {model!r} cannot be part of the request path; nothing was sent"
            )
        return self._url.replace("{model}", model)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        # No key, no header: "Bearer " with nothing after it is not a credential.
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _encode(self, picture: Picture) -> str | dict[str, str]:
        data = base64.b64encode(picture.payload.data).decode("ascii")
        if self._image_format is ImageFormat.BASE64_OBJECT:
            return {"content_type": MIME_TYPE, "base64": data}
        return f"data:{MIME_TYPE};base64,{data}"

    def _text_body(
        self, state: StateType, questions: Mapping[str, Question], model: str
    ) -> dict[str, Any]:
        """Everything the body carries apart from the pictures, unchecked."""
        return {
            **self._extra,
            "model": model,
            "state": state,
            "questions": {key: q.as_payload() for key, q in questions.items()},
        }

    def build_body(
        self,
        state: StateType,
        questions: Mapping[str, Question],
        model: str,
        pictures: Pictures | None,
    ) -> dict[str, Any]:
        """The JSON body, or JevValidationError for one that cannot be sent."""
        if not questions:
            raise JevValidationError("a request needs at least one question")
        boxes = [key for key, q in questions.items() if isinstance(q, Box)]
        if boxes:
            raise JevValidationError(
                f"the Clef dialect has no box question; {boxes} refused, nothing was sent"
            )
        ordered = pictures.ordered if pictures else []
        _check_image_count(len(ordered))
        body = self._text_body(state, questions, model)
        if ordered:
            body["images"] = [self._encode(picture) for picture in ordered]
        return body

    def body_size(
        self, state: StateType, questions: Mapping[str, Question], *, model: str | None = None
    ) -> int:
        # Measured without build_body's checks: a box is refused by ask(), where
        # the caller turns the refusal into a message, not by the estimate.
        return len(json_bytes(self._text_body(state, questions, model or self._model)))

    async def ask(
        self,
        state: StateType,
        questions: Mapping[str, Question],
        *,
        model: str | None = None,
        pictures: Pictures | None = None,
    ) -> JevResponse:
        model = model or self._model
        body = self.build_body(state, questions, model, pictures)
        url = self.endpoint(model)
        # The path of a Workers AI address carries the account id, which
        # diagnostics keeps out of sight, so an error names the server alone.
        server = str(URL(url).origin())
        # Encoded here rather than by the session, so the bytes sent are the bytes
        # body_size() measured whichever session this client was given.
        encoded = json_bytes(body)
        started = time.monotonic()
        try:
            # A redirect would post the house and its pictures somewhere the
            # entry never named. An answer is never a 3xx.
            async with self._session.post(
                url,
                data=encoded,
                headers=self._headers(),
                timeout=self._timeout,
                allow_redirects=False,
            ) as response:
                status = response.status
                retry_after = response.headers.get("Retry-After")
                raw = await _read_capped(response.content)
        except aiohttp.ClientError as err:
            reason = str(err).replace(url, server)
            raise JevConnectionError(f"request to {server} failed: {reason}") from err
        except TimeoutError as err:
            raise JevConnectionError(
                f"no answer from {server} within {self._timeout.total}s"
            ) from err
        latency_ms = (time.monotonic() - started) * 1000
        # JSON is UTF-8 (RFC 8259), whatever charset a proxy's header claims.
        text = raw.decode("utf-8", errors="replace")
        data = _loads(text)
        if status >= 300:
            raise _error_for(status, data, text, retry_after)
        return _finite(_parse(_unwrap(data, retry_after), model, latency_ms))

    async def async_close(self) -> None:
        """The session is Home Assistant's, so there is nothing of ours to close."""


async def _read_capped(stream: aiohttp.StreamReader) -> bytes:
    """The reply's bytes, or JevResponseError past MAX_REPLY_BYTES.

    StreamReader.read(n) hands back whatever has arrived, up to n bytes, so it
    is read until the end rather than once.
    """
    data = bytearray()
    while chunk := await stream.read(MAX_REPLY_BYTES + 1 - len(data)):
        data += chunk
        if len(data) > MAX_REPLY_BYTES:
            raise JevResponseError(
                f"the reply is over {MAX_REPLY_BYTES} bytes, far more than any answer; "
                "the rest was not read"
            )
    return bytes(data)


def _finite(response: JevResponse) -> JevResponse:
    """The response, or JevResponseError when an answer holds NaN or infinity.

    Python's JSON reader takes NaN, Infinity and 1e999. A verdict compares a
    noul with its cutoff, and every comparison with NaN is false, so the verdict
    would stay where it was while looking answered.
    """
    for key, answer in response.answers.items():
        if not _all_finite(dataclasses.asdict(answer)):
            raise JevResponseError(f"answer {key!r} holds a number that is not finite")
    return response


def _all_finite(value: Any) -> bool:
    # An answer is floats, strings and dicts of them; it holds no list.
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, Mapping):
        return all(_all_finite(item) for item in value.values())
    return True


def _loads(text: str) -> Any:
    """The body as JSON, or None for a body that is not, such as a proxy's HTML page."""
    with contextlib.suppress(ValueError):
        return json.loads(text)
    return None


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        # An HTTP date. Rare enough that "no hint" is the honest reading.
        return None


def _cf_errors(data: Any) -> list[tuple[int | None, str]]:
    """(code, message) for each entry in a Cloudflare errors[] list."""
    if not isinstance(data, Mapping):
        return []
    errors = data.get("errors")
    if not isinstance(errors, list):
        return []
    found: list[tuple[int | None, str]] = []
    for item in errors:
        if isinstance(item, Mapping):
            code = item.get("code")
            found.append((code if isinstance(code, int) else None, str(item.get("message", ""))))
        else:
            found.append((None, str(item)))
    return found


def _message(data: Any, text: str) -> str:
    """The server's own words: Cloudflare's errors, TypeSafe's detail, or the body."""
    if errors := _cf_errors(data):
        return "; ".join(
            f"{code}: {message}" if code is not None else message for code, message in errors
        )[:400]
    if isinstance(data, Mapping):
        for key in ("detail", "error"):
            value = data.get(key)
            if isinstance(value, Mapping):
                value = value.get("message")
            if isinstance(value, str) and value.strip():
                return value.strip()[:400]
    return text.strip()[:400]


def _error_for(status: int | None, data: Any, text: str, retry_after: str | None) -> JevError:
    """The jevclient exception for a failed reply, by Cloudflare code first.

    A Cloudflare code says more than the HTTP status it came with: 3036 and 3040
    are both 429, and only one of them goes away in a few seconds.
    """
    detail = _message(data, text)
    for code, _ in _cf_errors(data):
        if code in _CF_AUTH or code in _CF_FORBIDDEN:
            return JevAuthError(f"the credential was rejected: {detail}", status=status)
        if code in _CF_NO_CREDIT:
            return JevResponseError(f"HTTP 402: {detail}", status=402)
        if code in _CF_OVERLOADED:
            return JevOverloadedError(f"the service is saturated: {detail}", status=status)
        if code in _CF_TIMEOUT:
            return JevConnectionError(f"the server timed out: {detail}", status=status)
        if code in _CF_INVALID:
            return JevValidationError(
                f"the request was rejected as invalid: {detail}", status=status
            )
    if status is not None and 300 <= status < 400:
        return JevResponseError(
            f"HTTP {status}: the server sent the request to another address, and "
            "Mentat follows no redirect; give the entry the address itself",
            status=status,
        )
    if status in (401, 403):
        return JevAuthError(f"the credential was rejected: {detail}", status=status)
    if status in (400, 413, 422):
        return JevValidationError(f"the request was rejected as invalid: {detail}", status=status)
    if status == 429:
        return JevRateLimitError(f"rate limited: {detail}", retry_after=_retry_after(retry_after))
    if status == 529:
        return JevOverloadedError(f"the service is saturated: {detail}", status=status)
    if status is None:
        return JevResponseError(f"the server reported a failure: {detail}")
    return JevResponseError(f"HTTP {status}: {detail}", status=status)


def _unwrap(data: Any, retry_after: str | None) -> Any:
    """The result out of Cloudflare's envelope, or the body when it has none.

    Workers AI answers {"success", "result", "errors", "messages"}, and can say
    success: false with HTTP 200. A body with answers at the top is not wrapped.
    """
    if not isinstance(data, Mapping) or "answers" in data:
        return data
    if data.get("success") is False:
        raise _error_for(None, data, json.dumps(data), retry_after)
    if "result" in data:
        return data["result"]
    return data


def _parse(data: Any, model: str, latency_ms: float) -> JevResponse:
    if not isinstance(data, Mapping):
        raise JevResponseError(f"expected an object, got {type(data).__name__}")
    raw_answers = data.get("answers")
    if not isinstance(raw_answers, Mapping):
        raise WrongDialectError("the reply carries no answers object, in or out of an envelope")
    usage = data.get("usage")
    if not isinstance(usage, Mapping):
        usage = {}
    return JevResponse(
        model=str(data.get("model") or model),
        answers={key: parse_answer(key, raw) for key, raw in raw_answers.items()},
        usage=Usage(
            input_tokens=_token_count(usage, "input_tokens", "prompt_tokens"),
            output_tokens=_token_count(usage, "output_tokens", "completion_tokens"),
        ),
        latency_ms=latency_ms,
    )


def _token_count(usage: Mapping[str, Any], key: str, openai_key: str) -> int:
    """A usage count, or JevResponseError for one that is not a count.

    The budget adds this up, so a reply that bills null or "many" tokens is a
    reply that cannot be read, not a free call.
    """
    value = usage.get(key, usage.get(openai_key, 0))
    if isinstance(value, bool):
        value = None
    try:
        count = int(value)
    except (TypeError, ValueError, OverflowError):
        count = -1
    # A negative count would hand tokens back to the day's budget.
    if count < 0:
        raise JevResponseError(f"the reply's usage gives {key} as {value!r}, not a count")
    return count


def build_client(
    provider: Provider,
    *,
    api_key: str,
    base_url: str | None,
    model: str | None,
    account_id: str | None,
    image_format: ImageFormat | None,
    extra: Mapping[str, Any] | None,
    session: aiohttp.ClientSession,
) -> DecisionClient:
    """The client for this provider, pointed where the entry says.

    `provider` already carries the dialect (provider_for() folds the generic
    provider's in). {model} stays a template here because a request may name its
    own model.
    """
    url = resolve_url(provider, base_url, account_id, None)
    if provider.dialect is Dialect.TYPESAFE:
        return TypeSafeClient(api_key, session=session, base_url=url, model=model or provider.model)
    return ClefClient(
        api_key,
        session=session,
        url=url,
        model=model or provider.model,
        image_format=image_format or provider.image_format,
        extra=extra,
    )
