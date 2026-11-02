"""Both dialects against a mocked HTTP layer. No request leaves the process."""

from __future__ import annotations

import base64
import json
from typing import Any

import aiohttp
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.json import json_bytes
from jevclient import (
    Box,
    Choice,
    ChoiceAnswer,
    Image,
    JevAuthError,
    JevConnectionError,
    JevOverloadedError,
    JevRateLimitError,
    JevResponseError,
    JevValidationError,
    Noul,
    NoulAnswer,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)
from yarl import URL

from custom_components.mentat.client import (
    ClefClient,
    TypeSafeClient,
    WrongDialectError,
    build_client,
)
from custom_components.mentat.pictures import Picture, Pictures
from custom_components.mentat.providers import PROVIDERS, Dialect, ImageFormat

from .conftest import ACCOUNT_ID, API_KEY, LOCAL_URL

JPEG = b"\xff\xd8\xff\xe0 not a real jpeg, the client never decodes it"
TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
LOCAL_ENDPOINT = f"{LOCAL_URL}/v1/systemone"
CF_TEMPLATE = (
    f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/run/@cf/cloudflare/{{model}}"
)
QUESTIONS = {"door_open": Noul("Is the front door open?")}

# A reply as the published API shapes it: answers at the top.
PLAIN_REPLY: dict[str, Any] = {
    "model": "clef-flash-2026-10",
    "answers": {"door_open": {"type": "noul", "noul": 0.12}},
    "usage": {"input_tokens": 88, "output_tokens": 1},
}
# The same reply in Cloudflare's envelope.
ENVELOPED_REPLY: dict[str, Any] = {
    "success": True,
    "errors": [],
    "messages": [],
    "result": PLAIN_REPLY,
}


@pytest.fixture
def http(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    """Home Assistant's own HTTP mock: an unregistered request fails the test."""
    return aioclient_mock


@pytest.fixture
def session(hass: HomeAssistant, http: AiohttpClientMocker) -> aiohttp.ClientSession:
    return async_get_clientsession(hass)


def _sent(http: AiohttpClientMocker, url: str) -> dict[str, Any]:
    """The body and headers of the one request posted to `url`."""
    calls = [call for call in http.mock_calls if call[1] == URL(url)]
    assert len(calls) == 1
    _, _, body, headers = calls[0]
    # The Clef client posts the bytes it encoded; jevclient posts json=.
    if isinstance(body, bytes):
        body = json.loads(body)
    return {"json": body, "raw": calls[0][2], "headers": headers or {}}


def _pictures(count: int, dialect: Dialect = Dialect.CLEF) -> Pictures:
    """`count` copies of one fake JPEG. The client never decodes them."""
    pictures = Pictures.empty(dialect)
    for _ in range(count):
        pictures.add({}, Picture(payload=Image(JPEG, "jpg"), sent=(640, 480), source=(1920, 1440)))
    return pictures


def _clef(
    session: aiohttp.ClientSession,
    *,
    url: str = LOCAL_URL,
    api_key: str = "",
    image_format: ImageFormat = ImageFormat.DATA_URL,
    extra: dict[str, Any] | None = None,
) -> ClefClient:
    return ClefClient(
        api_key,
        session=session,
        url=url,
        model="clef-flash",
        image_format=image_format,
        extra=extra,
    )


# TypeSafe dialect


async def test_typesafe_passes_the_request_through(session, http) -> None:
    http.post(
        TYPESAFE_URL,
        json={
            "model": "jev-1.14.0",
            "answers": {
                "door_open": {"type": "noul", "noul": 0.9},
                "mode": {
                    "type": "choice",
                    "choice": "away",
                    "probabilities": {"away": 0.8, "home": 0.2},
                    "confidence": 0.7,
                },
            },
            "usage": {"input_tokens": 400, "output_tokens": 2},
        },
    )
    client = TypeSafeClient(
        API_KEY, session=session, base_url="https://api.typesafe.ai", model="jev-latest"
    )
    assert client.dialect is Dialect.TYPESAFE
    pictures = _pictures(1, Dialect.TYPESAFE)
    (reference,) = pictures.assets
    state = {"images": [{"name": "Front door", "image": reference}]}

    response = await client.ask(state, QUESTIONS, pictures=pictures)

    assert response.model == "jev-1.14.0"
    assert response.answers["door_open"] == NoulAnswer(noul=0.9)
    assert isinstance(response.answers["mode"], ChoiceAnswer)
    assert response.usage.input_tokens == 400
    sent = _sent(http, TYPESAFE_URL)
    assert sent["json"]["model"] == "jev-latest"
    assert sent["json"]["state"] == state
    assert sent["json"]["assets"][reference]["format"] == "jpg"
    assert sent["headers"]["Authorization"] == f"Bearer {API_KEY}"
    assert sent["headers"]["TypeSafe-Danger-Zone-Images"] == "true"
    await client.async_close()


async def test_typesafe_without_images_sends_no_assets(session, http) -> None:
    http.post(TYPESAFE_URL, json=PLAIN_REPLY)
    client = TypeSafeClient(
        API_KEY, session=session, base_url="https://api.typesafe.ai", model="jev-latest"
    )
    await client.ask("ok", QUESTIONS, model="jev-1.13.0", pictures=Pictures.empty(Dialect.TYPESAFE))
    sent = _sent(http, TYPESAFE_URL)
    assert "assets" not in sent["json"]
    assert sent["json"]["model"] == "jev-1.13.0"


async def test_typesafe_turns_a_local_refusal_into_a_validation_error(session, http) -> None:
    """jevclient raises ValueError for an asset nothing refers to; nothing is sent."""
    client = TypeSafeClient(
        API_KEY, session=session, base_url="https://api.typesafe.ai", model="jev-latest"
    )
    with pytest.raises(JevValidationError, match="nothing was sent"):
        await client.ask("no reference here", QUESTIONS, pictures=_pictures(1, Dialect.TYPESAFE))
    assert http.call_count == 0


async def test_typesafe_reads_a_cloudflare_wrapper_as_the_wrong_dialect(session, http) -> None:
    """The real JevClient's own message is what the check keys on."""
    http.post(TYPESAFE_URL, json=ENVELOPED_REPLY)
    client = TypeSafeClient(
        API_KEY, session=session, base_url="https://api.typesafe.ai", model="jev-latest"
    )
    with pytest.raises(WrongDialectError):
        await client.ask("ok", QUESTIONS)


async def test_typesafe_passes_other_errors_through(session, http) -> None:
    http.post(TYPESAFE_URL, status=500, text='{"detail": "boom"}')
    client = TypeSafeClient(
        API_KEY, session=session, base_url="https://api.typesafe.ai", model="jev-latest"
    )
    with pytest.raises(JevResponseError) as caught:
        await client.ask("ok", QUESTIONS)
    assert not isinstance(caught.value, WrongDialectError)
    assert caught.value.status == 500


async def test_typesafe_refuses_more_than_four_images(session, http) -> None:
    client = TypeSafeClient(
        API_KEY, session=session, base_url="https://api.typesafe.ai", model="jev-latest"
    )
    with pytest.raises(JevValidationError, match="at most 4 images"):
        await client.ask("ok", QUESTIONS, pictures=_pictures(5, Dialect.TYPESAFE))
    assert http.call_count == 0


# Clef dialect: replies


async def test_clef_reads_a_plain_reply(session, http) -> None:
    http.post(LOCAL_ENDPOINT, json=PLAIN_REPLY)
    client = _clef(session, extra={"truncate": False})
    assert client.dialect is Dialect.CLEF

    response = await client.ask("front door closed", QUESTIONS)

    assert response.answers["door_open"] == NoulAnswer(noul=0.12)
    assert response.model == "clef-flash-2026-10"
    assert response.usage.input_tokens == 88
    assert response.latency_ms >= 0
    sent = _sent(http, LOCAL_ENDPOINT)
    assert sent["json"] == {
        "truncate": False,
        "model": "clef-flash",
        "state": "front door closed",
        "questions": {"door_open": {"type": "noul", "instructions": "Is the front door open?"}},
    }
    # No key, no header.
    assert "Authorization" not in sent["headers"]
    await client.async_close()


async def test_clef_unwraps_what_cloudflare_wraps_around_the_reply(session, http) -> None:
    endpoint = CF_TEMPLATE.replace("{model}", "clef")
    http.post(endpoint, json=ENVELOPED_REPLY)
    client = _clef(session, url=CF_TEMPLATE, api_key=API_KEY)

    response = await client.ask("ok", QUESTIONS, model="clef")

    assert response.answers["door_open"] == NoulAnswer(noul=0.12)
    assert _sent(http, endpoint)["headers"]["Authorization"] == f"Bearer {API_KEY}"


async def test_clef_takes_openai_style_usage_and_the_requested_model(session, http) -> None:
    http.post(
        LOCAL_ENDPOINT,
        json={
            "answers": {"door_open": {"type": "noul", "noul": 0.5}},
            "usage": {"prompt_tokens": 70, "completion_tokens": 1},
        },
    )
    response = await _clef(session).ask("ok", QUESTIONS)
    assert response.usage.input_tokens == 70
    assert response.usage.output_tokens == 1
    assert response.model == "clef-flash"


async def test_clef_tolerates_a_reply_without_usage(session, http) -> None:
    http.post(
        LOCAL_ENDPOINT,
        json={"answers": {"door_open": {"type": "noul", "noul": 0.5}}, "usage": 3},
    )
    response = await _clef(session).ask("ok", QUESTIONS)
    assert response.usage.input_tokens == 0


_ANSWERS = {"door_open": {"type": "noul", "noul": 0.5}}


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": None, "output_tokens": 1},
        {"input_tokens": 12, "output_tokens": None},
        {"input_tokens": "many", "output_tokens": 1},
        {"prompt_tokens": True, "completion_tokens": 1},
    ],
    ids=["null-input", "null-output", "word", "bool"],
)
@pytest.mark.parametrize("dialect", ["typesafe", "clef"])
async def test_a_usage_count_that_is_not_a_number_is_a_bad_reply(
    session, http, dialect: str, usage: dict[str, Any]
) -> None:
    """The budget adds these up, so a reply that cannot say what it cost is refused."""
    reply = {"answers": _ANSWERS, "usage": usage}
    if dialect == "typesafe":
        http.post(TYPESAFE_URL, json=reply)
        client: Any = TypeSafeClient(
            API_KEY,
            session=session,
            base_url="https://api.typesafe.ai",
            model="jev-latest",
        )
    else:
        http.post(LOCAL_ENDPOINT, json=reply)
        client = _clef(session)
    with pytest.raises(JevResponseError, match="not a number"):
        await client.ask("ok", QUESTIONS)


async def test_typesafe_tolerates_a_reply_without_usage(session, http) -> None:
    http.post(TYPESAFE_URL, json={"answers": _ANSWERS, "usage": 3})
    client = TypeSafeClient(
        API_KEY, session=session, base_url="https://api.typesafe.ai", model="jev-latest"
    )
    response = await client.ask("ok", QUESTIONS)
    assert (response.usage.input_tokens, response.usage.output_tokens) == (0, 0)


async def test_clef_sends_the_bytes_the_budget_measured(session, http) -> None:
    http.post(LOCAL_ENDPOINT, json=PLAIN_REPLY)
    state = {"note": "café open", "when": "2026-10-30 22:41 Thursday"}
    await _clef(session).ask(state, QUESTIONS)
    sent = _sent(http, LOCAL_ENDPOINT)
    assert len(sent["raw"]) == _clef(session).body_size(state, QUESTIONS)
    assert sent["headers"]["Content-Type"] == "application/json"


async def test_clef_extra_cannot_replace_the_body(session, http) -> None:
    http.post(LOCAL_ENDPOINT, json=PLAIN_REPLY)
    client = _clef(session, extra={"model": "other", "state": "x", "top_k": 3})
    await client.ask("real state", QUESTIONS)
    sent = _sent(http, LOCAL_ENDPOINT)["json"]
    assert sent["model"] == "clef-flash"
    assert sent["state"] == "real state"
    assert sent["top_k"] == 3


async def test_clef_success_false_5006_is_a_validation_error(session, http) -> None:
    """Workers AI can refuse with HTTP 200 and success: false."""
    http.post(
        LOCAL_ENDPOINT,
        json={
            "success": False,
            "result": None,
            "errors": [{"code": 5006, "message": "Error: oneOf at '/' not met"}],
            "messages": [],
        },
    )
    with pytest.raises(JevValidationError, match="5006"):
        await _clef(session).ask("ok", QUESTIONS)


async def test_clef_success_false_with_an_unknown_code(session, http) -> None:
    http.post(LOCAL_ENDPOINT, json={"success": False, "errors": ["something odd"]})
    with pytest.raises(JevResponseError, match="something odd"):
        await _clef(session).ask("ok", QUESTIONS)


async def test_clef_reply_without_answers_is_the_wrong_dialect(session, http) -> None:
    http.post(LOCAL_ENDPOINT, json={"success": True, "result": {"response": "hi"}})
    with pytest.raises(WrongDialectError):
        await _clef(session).ask("ok", QUESTIONS)


async def test_clef_object_without_answers_or_wrapper(session, http) -> None:
    http.post(LOCAL_ENDPOINT, json={"response": "free text from a chat model"})
    with pytest.raises(WrongDialectError):
        await _clef(session).ask("ok", QUESTIONS)


async def test_clef_reply_that_is_not_an_object(session, http) -> None:
    http.post(LOCAL_ENDPOINT, text="<html>proxy page</html>")
    with pytest.raises(JevResponseError, match="expected an object"):
        await _clef(session).ask("ok", QUESTIONS)


async def test_clef_unreadable_answer(session, http) -> None:
    http.post(LOCAL_ENDPOINT, json={"answers": {"door_open": {"type": "noul"}}})
    with pytest.raises(JevResponseError, match="not readable"):
        await _clef(session).ask("ok", QUESTIONS)


# Clef dialect: statuses and Cloudflare codes


@pytest.mark.parametrize(
    ("status", "body", "error", "status_after"),
    [
        (401, '{"detail": "bad key"}', JevAuthError, 401),
        (403, '{"error": {"message": "forbidden"}}', JevAuthError, 403),
        (400, "bad request", JevValidationError, 400),
        (413, "too large", JevValidationError, 413),
        (422, "[]", JevValidationError, 422),
        (529, "busy", JevOverloadedError, 529),
        (500, "internal", JevResponseError, 500),
        (502, '{"detail": {"message": "upstream"}}', JevResponseError, 502),
    ],
)
async def test_clef_maps_http_statuses(
    session, http, status: int, body: str, error: type[Exception], status_after: int
) -> None:
    http.post(LOCAL_ENDPOINT, status=status, text=body)
    with pytest.raises(error) as caught:
        await _clef(session).ask("ok", QUESTIONS)
    assert caught.value.status == status_after  # type: ignore[attr-defined]


async def test_clef_429_carries_retry_after(session, http) -> None:
    http.post(LOCAL_ENDPOINT, status=429, text="slow down", headers={"Retry-After": "7"})
    with pytest.raises(JevRateLimitError) as caught:
        await _clef(session).ask("ok", QUESTIONS)
    assert caught.value.retry_after == 7.0


@pytest.mark.parametrize("header", [{}, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}])
async def test_clef_429_without_a_usable_retry_after(session, http, header) -> None:
    http.post(LOCAL_ENDPOINT, status=429, text="slow down", headers=header)
    with pytest.raises(JevRateLimitError) as caught:
        await _clef(session).ask("ok", QUESTIONS)
    assert caught.value.retry_after is None


@pytest.mark.parametrize(
    ("status", "code", "error", "status_after"),
    [
        (400, 10000, JevAuthError, 400),
        (403, 5018, JevAuthError, 403),
        (400, 5007, JevValidationError, 400),
        (404, 3042, JevValidationError, 404),
        (429, 3040, JevOverloadedError, 429),
        (429, 3036, JevResponseError, 402),
        (408, 3007, JevConnectionError, 408),
        (418, 1, JevResponseError, 418),
    ],
)
async def test_clef_maps_cloudflare_codes_before_statuses(
    session, http, status: int, code: int, error: type[Exception], status_after: int
) -> None:
    http.post(
        LOCAL_ENDPOINT,
        status=status,
        json={"success": False, "errors": [{"code": code, "message": "said so"}]},
    )
    with pytest.raises(error) as caught:
        await _clef(session).ask("ok", QUESTIONS)
    assert caught.value.status == status_after  # type: ignore[attr-defined]
    assert f"{code}: said so" in str(caught.value)


async def test_clef_network_failure(session, http) -> None:
    http.post(LOCAL_ENDPOINT, exc=aiohttp.ClientConnectionError("refused"))
    with pytest.raises(JevConnectionError, match="refused"):
        await _clef(session).ask("ok", QUESTIONS)


async def test_clef_timeout(session, http) -> None:
    http.post(LOCAL_ENDPOINT, exc=TimeoutError())
    with pytest.raises(JevConnectionError, match="no answer"):
        await _clef(session).ask("ok", QUESTIONS)


# Clef dialect: what is refused before sending


async def test_clef_refuses_a_box_without_a_request(session, http) -> None:
    with pytest.raises(JevValidationError, match="no box question"):
        await _clef(session).ask(
            "ok", {"parcel": Box("the parcel in the first image")}, pictures=_pictures(1)
        )
    assert http.call_count == 0


async def test_clef_refuses_no_questions(session, http) -> None:
    with pytest.raises(JevValidationError, match="at least one question"):
        await _clef(session).ask("ok", {})
    assert http.call_count == 0


async def test_clef_refuses_more_than_four_images(session, http) -> None:
    with pytest.raises(JevValidationError, match="at most 4 images"):
        await _clef(session).ask("ok", QUESTIONS, pictures=_pictures(5))
    assert http.call_count == 0


# Clef dialect: image encoding


async def test_clef_sends_data_urls(session, http) -> None:
    http.post(LOCAL_ENDPOINT, json=PLAIN_REPLY)
    await _clef(session).ask("ok", QUESTIONS, pictures=_pictures(2))
    images = _sent(http, LOCAL_ENDPOINT)["json"]["images"]
    expected = "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()
    assert images == [expected, expected]


async def test_clef_sends_base64_objects(session, http) -> None:
    http.post(LOCAL_ENDPOINT, json=PLAIN_REPLY)
    client = _clef(session, image_format=ImageFormat.BASE64_OBJECT)
    await client.ask("ok", QUESTIONS, pictures=_pictures(1))
    images = _sent(http, LOCAL_ENDPOINT)["json"]["images"]
    assert images == [{"content_type": "image/jpeg", "base64": base64.b64encode(JPEG).decode()}]


async def test_clef_reads_assets_as_data_urls(session, http) -> None:
    """Assets are a TypeSafe idea; a Clef client given them sends data URLs."""
    http.post(LOCAL_ENDPOINT, json=PLAIN_REPLY)
    client = _clef(session, image_format=ImageFormat.ASSETS)
    await client.ask("ok", QUESTIONS, pictures=_pictures(1))
    images = _sent(http, LOCAL_ENDPOINT)["json"]["images"]
    assert images[0].startswith("data:image/jpeg;base64,")


async def test_clef_omits_the_images_key_without_pictures(session, http) -> None:
    http.post(LOCAL_ENDPOINT, json=PLAIN_REPLY)
    await _clef(session).ask("ok", QUESTIONS, pictures=Pictures.empty(Dialect.CLEF))
    assert "images" not in _sent(http, LOCAL_ENDPOINT)["json"]


# Body size


def _typesafe(session: aiohttp.ClientSession) -> TypeSafeClient:
    return TypeSafeClient(
        API_KEY, session=session, base_url="https://api.typesafe.ai", model="jev-latest"
    )


def test_typesafe_body_size_is_the_encoded_body(session) -> None:
    questions = {
        "lights_left_on": Noul("Did someone leave the hall lights on?"),
        "mode": Choice("Which mode fits?", {"away": None, "night": "Everyone asleep"}),
    }
    state = {"now": "2026-11-01 07:15 Sunday", "entities": [{"state": "off"}]}
    wire = {
        "state": state,
        "model": "jev-latest",
        "questions": {key: q.as_payload() for key, q in questions.items()},
    }
    assert _typesafe(session).body_size(state, questions) == len(json_bytes(wire))


def test_typesafe_body_size_uses_the_model_it_is_given(session) -> None:
    client = _typesafe(session)
    longer = client.body_size("ok", QUESTIONS, model="jev-latest-2026")
    assert longer - client.body_size("ok", QUESTIONS) == len("-2026")


@pytest.mark.parametrize("dialect", list(Dialect))
def test_body_size_counts_an_accent_as_its_utf8_bytes(session, dialect: Dialect) -> None:
    client: Any = _typesafe(session) if dialect is Dialect.TYPESAFE else _clef(session)
    plain = client.body_size("creme", QUESTIONS)
    assert client.body_size("crème", QUESTIONS) - plain == 1


def test_clef_body_size_carries_extra_and_measures_a_box(session) -> None:
    """The estimate never refuses: ask() does, where the caller can say why."""
    questions = {"bin": Box("the wheelie bin")}
    client = _clef(session, extra={"top_k": 3})
    body = {
        "top_k": 3,
        "model": "clef-flash",
        "state": "ok",
        "questions": {"bin": questions["bin"].as_payload()},
    }
    assert client.body_size("ok", questions) == len(json_bytes(body))


# build_client


def test_build_client_picks_the_dialect(session) -> None:
    typesafe = build_client(
        PROVIDERS["typesafe"],
        api_key=API_KEY,
        base_url=None,
        model=None,
        account_id=None,
        image_format=None,
        extra=None,
        session=session,
    )
    assert isinstance(typesafe, TypeSafeClient)

    cloudflare = build_client(
        PROVIDERS["cloudflare"],
        api_key=API_KEY,
        base_url=None,
        model="clef",
        account_id=ACCOUNT_ID,
        image_format=None,
        extra=None,
        session=session,
    )
    assert isinstance(cloudflare, ClefClient)
    assert cloudflare.endpoint("clef") == CF_TEMPLATE.replace("{model}", "clef")

    local = build_client(
        PROVIDERS["local_clef"],
        api_key="",
        base_url=f"{LOCAL_URL}/",
        model=None,
        account_id=None,
        image_format=ImageFormat.BASE64_OBJECT,
        extra={"truncate": False},
        session=session,
    )
    assert isinstance(local, ClefClient)
    assert local.endpoint("clef-flash") == LOCAL_ENDPOINT
