"""The six actions, and the one way every request reaches a server.

`mentat.noul`, `mentat.choice` and `mentat.score` ask one question each, which
is as much as most automations need. `mentat.ask` sends several questions about
one state in one request. `mentat.box` asks where something is in one picture.
`mentat.calibrate` sends nothing: it reads the recorder and suggests a cutoff
and a release for a noul.

`async_ask` is the only place a request is sent from outside the coordinator.
The actions, the editor's trial answer and the AI Task entity all call it. It
estimates the request, refuses one that would not fit the day's budget,
reserves the estimate while the request is in flight, sends it and records
what the server billed.

An action sends only what its caller may read. A user who is no administrator
may not send a file path, a camera or a target entity hidden from them, and a
template they write goes to the model as written, unrendered. Nothing is
fetched, read from the recorder or sent before those checks pass.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine, Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.auth.models import User
from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.const import CONF_MODEL, CONF_TYPE, STATE_ON
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.target import (
    TargetSelection,
    async_extract_referenced_entity_ids,
)
from homeassistant.util import dt as dt_util
from jevclient import (
    MAX_CHOICE_OPTIONS,
    MAX_SCORE_LEVELS,
    MIN_CHOICE_OPTIONS,
    MIN_SCORE_LEVELS,
    Answer,
    Box,
    BoxAnswer,
    Choice,
    ChoiceAnswer,
    JevAuthError,
    JevError,
    JevRateLimitError,
    JevResponse,
    JevValidationError,
    NoulAnswer,
    Question,
    ScoreAnswer,
)

from .calibrate import (
    RecorderUnreadableError,
    async_recorded_states,
    calibration,
    number,
    pieces_of,
)
from .const import (
    ATTR_ANSWERS,
    ATTR_CONFIDENT,
    ATTR_CONFIG_ENTRY,
    ATTR_LATENCY_MS,
    ATTR_QUESTIONS,
    ATTR_USAGE,
    CONF_BRIEF,
    CONF_CUTOFF,
    CONF_DAYS,
    CONF_FALLBACK,
    CONF_GRACE,
    CONF_HIGH,
    CONF_INSTRUCTIONS,
    CONF_LOW,
    CONF_MAX_IMAGES,
    CONF_MIN_CONFIDENCE,
    CONF_NOTE,
    CONF_OPTIONS,
    CONF_PICTURE,
    CONF_PICTURES,
    CONF_RELEASE,
    CONF_SCALE,
    CONF_SENSOR,
    CONF_TRUTH,
    CONF_TRUTH_STATE,
    CONF_WINDOW,
    CONF_WITH_ATTRIBUTES,
    DEFAULT_CALIBRATE_DAYS,
    DEFAULT_MAX_IMAGES,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
    MAX_CALIBRATE_CHANGES,
    MAX_CALIBRATE_DAYS,
    MAX_SCAN_INTERVAL_SECONDS,
    SERVICE_ASK,
    SERVICE_BOX,
    SERVICE_CALIBRATE,
    SERVICE_CHOICE,
    SERVICE_NOUL,
    SERVICE_SCORE,
    TYPE_BOX,
    TYPE_CHOICE,
    TYPE_NOUL,
    TYPE_SCORE,
)
from .judge import confident
from .pictures import (
    Pictures,
    async_request_images,
    attach,
    is_entity_source,
    is_path_source,
)
from .providers import model_offered
from .readings import async_state
from .schema import ENTRY, MIN_CONFIDENCE, PROBABILITY, WINDOW_SCHEMA, WORDING, floor_problem
from .watches import answer_as_dict, options_from, question_from_mapping, window_from_mapping
from .window import async_window_state_once

if TYPE_CHECKING:
    from . import MentatConfigEntry

_LOGGER = logging.getLogger(__name__)

# What the target picker puts in call.data.
TARGET_KEYS = ("entity_id", "device_id", "area_id", "floor_id", "label_id")
# The key one-question actions ask under. The caller never sees it.
ANSWER_KEY = "answer"

# What any action that reads the house may carry. Picture sources are camera or
# image entity ids, or absolute file paths; pictures.py tells them apart.
_READS: dict[Any, Any] = {
    vol.Optional(CONF_NOTE): ENTRY,
    vol.Optional(CONF_PICTURES, default=list): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional(CONF_WITH_ATTRIBUTES, default=False): cv.boolean,
    **cv.TARGET_SERVICE_FIELDS,
}
_ROUTING: dict[Any, Any] = {
    vol.Optional(ATTR_CONFIG_ENTRY): cv.string,
    vol.Optional(CONF_MODEL): cv.string,
}
_WORDED: dict[Any, Any] = {
    vol.Required(CONF_INSTRUCTIONS): ENTRY,
    vol.Optional(CONF_BRIEF): ENTRY,
}

NOUL_SCHEMA = vol.Schema(
    _READS
    | _ROUTING
    | _WORDED
    | {
        vol.Optional(CONF_HIGH): WORDING,
        vol.Optional(CONF_LOW): WORDING,
        vol.Optional(CONF_CUTOFF, default=0.5): PROBABILITY,
    }
)
CHOICE_SCHEMA = vol.Schema(
    _READS
    | _ROUTING
    | _WORDED
    | {
        # Names alone, or each name with what it means.
        vol.Required(CONF_OPTIONS): vol.All(
            vol.Any(
                vol.All(cv.ensure_list, [cv.string]),
                {cv.string: vol.Any(ENTRY, None)},
            ),
            options_from,
        ),
        vol.Optional(CONF_MIN_CONFIDENCE): MIN_CONFIDENCE,
    }
)
SCORE_SCHEMA = vol.Schema(
    _READS
    | _ROUTING
    | _WORDED
    | {
        vol.Required(CONF_SCALE): vol.All(cv.ensure_list, [WORDING]),
        vol.Optional(CONF_MIN_CONFIDENCE): PROBABILITY,
    }
)
ASK_SCHEMA = vol.Schema(
    _READS
    | _ROUTING
    | {
        # No questions would still be a billed request, answered with nothing.
        vol.Required(ATTR_QUESTIONS): vol.All(vol.Schema({cv.string: dict}), vol.Length(min=1)),
        vol.Optional(CONF_WINDOW): WINDOW_SCHEMA,
    }
)
# One picture and no state: the edges are fractions of that picture alone.
BOX_SCHEMA = vol.Schema(_ROUTING | _WORDED | {vol.Required(CONF_PICTURE): cv.string})
# Nothing is sent, so no entry, no model and no budget.
CALIBRATE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_SENSOR): cv.entity_id,
        vol.Required(CONF_TRUTH): cv.entity_id,
        vol.Optional(CONF_TRUTH_STATE, default=STATE_ON): cv.string,
        vol.Optional(CONF_DAYS, default=DEFAULT_CALIBRATE_DAYS): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=MAX_CALIBRATE_DAYS)
        ),
        vol.Optional(CONF_GRACE, default=DEFAULT_SCAN_INTERVAL_SECONDS): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=MAX_SCAN_INTERVAL_SECONDS)
        ),
    }
)


def _invalid(key: str, /, **placeholders: str) -> ServiceValidationError:
    """The caller's mistake, found before anything was sent."""
    return ServiceValidationError(
        translation_domain=DOMAIN, translation_key=key, translation_placeholders=placeholders
    )


def _failed(key: str, /, **placeholders: str) -> HomeAssistantError:
    """A request that went out, or was about to, and did not come back answered."""
    return HomeAssistantError(
        translation_domain=DOMAIN, translation_key=key, translation_placeholders=placeholders
    )


def resolve_entry(hass: HomeAssistant, wanted: str | None) -> MentatConfigEntry:
    """The loaded entry a call asks with: the one it names, or the only one.

    Each entry has its own key and its own budget. With several loaded and none
    named, any pick would bill an account the caller did not choose.
    """
    loaded: dict[str, MentatConfigEntry] = {
        entry.entry_id: entry for entry in hass.config_entries.async_loaded_entries(DOMAIN)
    }
    if wanted:
        if wanted not in loaded:
            raise _invalid("no_entry")
        return loaded[wanted]
    match list(loaded.values()):
        case []:
            raise _invalid("no_entry")
        case [only]:
            return only
        case several:
            titles = ", ".join(sorted(entry.title for entry in several))
            raise _invalid("entry_ambiguous", entries=titles)


def _refusal(
    err: JevError, entry: MentatConfigEntry, questions: Mapping[str, Question], model: str | None
) -> HomeAssistantError:
    """What a failed request tells the caller.

    A validation error on a box most likely means the model cannot draw one:
    Clef has no box question and early Jev releases refuse it. Naming the model
    tells the user which setting to change.
    """
    runtime = entry.runtime_data
    reason = str(err)
    if isinstance(err, JevValidationError) and any(isinstance(q, Box) for q in questions.values()):
        return _failed("box_refused", model=model or runtime.model, reason=reason.rstrip("."))
    key = "ask_failed"
    if isinstance(err, JevAuthError):
        key = "key_refused"
    elif isinstance(err, JevRateLimitError):
        key = "rate_limited"
    return _failed(key, provider=runtime.provider.title, reason=reason)


async def async_ask(
    hass: HomeAssistant,
    entry: MentatConfigEntry,
    state: Any,
    questions: Mapping[str, Question],
    *,
    pictures: Pictures | None = None,
    model: str | None = None,
    task: str | None = None,
) -> JevResponse:
    """Send one request, if it fits today's budget, and book what it cost.

    The client measures the body in its own dialect. Pictures are billed by
    their pixels rather than their bytes, so their tokens are added on top.
    `model` replaces the entry's model for this request only; `task` names the
    AI Task asking, for the refusal.
    """
    runtime = entry.runtime_data
    ledger = runtime.ledger
    ledger.roll_over(dt_util.now().date())
    picture_tokens = 0 if pictures is None else pictures.tokens
    body = runtime.client.body_size(state, questions, model=model)
    estimate = ledger.estimate(body, picture_tokens)
    if not ledger.fits(estimate):
        raise over_budget(entry, estimate, task)
    try:
        with ledger.reserve(estimate):
            response = await runtime.client.ask(state, questions, model=model, pictures=pictures)
    except JevError as err:
        # A model the caller named can be refused with 401 or 403 for that model
        # alone. The entry's own model will say on its next request whether the
        # key itself is bad.
        if isinstance(err, JevAuthError) and model is None:
            entry.async_start_reauth(hass)
        raise _refusal(err, entry, questions, model) from err
    ledger.record(response.usage.input_tokens, body, picture_tokens)
    if model is None:
        # An override answers for itself, not for the entry's model.
        runtime.model_version = response.model or runtime.model_version
    ledger.notify()
    return response


def over_budget(
    entry: MentatConfigEntry, estimate: int, task: str | None = None
) -> HomeAssistantError:
    """The refusal of a request that would not fit today's budget.

    `task` names the AI Task asking, so the message says which task it was.
    """
    ledger = entry.runtime_data.ledger
    spend = {
        "estimate": str(estimate),
        "remaining": str(ledger.remaining()),
        "budget": str(ledger.budget),
    }
    if task is not None:
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="task_over_budget",
            translation_placeholders={"task": task, **spend},
        )
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="request_over_budget",
        translation_placeholders=spend,
    )


def check_room(entry: MentatConfigEntry, task: str | None = None) -> None:
    """Refuse at once when not even the smallest request fits today's budget.

    Fetching and resizing pictures is slow and wakes cameras, so a spent budget
    is refused before any of it. async_ask still checks the real estimate.
    """
    ledger = entry.runtime_data.ledger
    ledger.roll_over(dt_util.now().date())
    if not ledger.has_room():
        raise over_budget(entry, ledger.estimate(0), task)


def max_images(entry: MentatConfigEntry) -> int:
    """Images one request of this entry may carry, from the options."""
    return int(entry.options.get(CONF_MAX_IMAGES, DEFAULT_MAX_IMAGES))


@dataclass(frozen=True, slots=True)
class _Caller:
    """Who an action call runs as, looked up once per call.

    `unlimited` is a call with no user behind it, or an administrator's.
    """

    user: User | None
    unlimited: bool


async def _caller(hass: HomeAssistant, call: ServiceCall) -> _Caller:
    """The user behind a call, and whether the caller checks apply to them.

    An automation run gets a fresh context with no user, and its author was an
    administrator, so it is not limited. A script carries the context it was
    started with: one run from a user's dashboard button, or with their token,
    runs as that user and is checked like them. A user id that no longer
    resolves is limited and may read nothing.
    """
    user_id = call.context.user_id
    if user_id is None:
        return _Caller(user=None, unlimited=True)
    user = await hass.auth.async_get_user(user_id)
    return _Caller(user=user, unlimited=user is not None and user.is_admin)


def _check_caller_may_read(
    hass: HomeAssistant,
    caller: _Caller,
    sources: Iterable[str],
    selector: Mapping[str, Any] | None = None,
    *,
    entities: Iterable[str] = (),
) -> None:
    """Refuse a source, a target or an entity the user behind the call may not read.

    Whatever is fetched goes to the model and comes back as an answer about it,
    so a picture from a camera a user cannot see, or the state of an entity
    hidden from them, would leak through the response. A file path reads any
    file Home Assistant may, which only an administrator can browse. See
    _caller() for who is limited.

    The target is resolved the way the state and the window resolve it, groups
    expanded, and checked before anything is read from it or the recorder.
    `entities` are checked as written: their own history is what gets read, and
    expanding a group would check its members and never the group.
    """
    if caller.unlimited:
        return
    user = caller.user

    def refuse_entity(entity_id: str) -> None:
        if user is None or not user.permissions.check_entity(entity_id, POLICY_READ):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="entity_not_permitted",
                translation_placeholders={"entity_id": entity_id},
            )

    for entity_id in entities:
        refuse_entity(entity_id)
    for source in sources:
        if is_path_source(source):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="path_needs_admin",
                translation_placeholders={"path": source},
            )
        # Anything else is refused as no picture source when it is fetched.
        if is_entity_source(source):
            refuse_entity(source)
    if selector:
        selected = async_extract_referenced_entity_ids(
            hass, TargetSelection(dict(selector)), expand_group=True
        )
        # As written: a registry id in place of an entity id matches nothing
        # here, so its entity is refused without being named.
        named = {str(item) for item in cv.ensure_list(selector.get("entity_id"))}
        for entity_id in sorted(selected.referenced | selected.indirectly_referenced):
            if entity_id in named:
                refuse_entity(entity_id)
            elif user is None or not user.permissions.check_entity(entity_id, POLICY_READ):
                # An id the caller did not write came out of an area, a device,
                # a label or a group, and naming it would show them an entity
                # their permissions hide.
                raise _invalid("target_not_permitted")


@dataclass(frozen=True, slots=True)
class _Call:
    """One action call as the request paths read it, taken from call.data once."""

    entry: MentatConfigEntry
    caller: _Caller
    target: dict[str, Any]
    note: Any
    pictures: tuple[str, ...]
    model: str | None
    with_attributes: bool
    window: Mapping[str, Any] | None


async def _read_call(hass: HomeAssistant, call: ServiceCall) -> _Call:
    data = call.data
    # mentat.box names one picture; every other action has a list, empty by default.
    sources = data[CONF_PICTURES] if CONF_PICTURES in data else [data[CONF_PICTURE]]
    entry = resolve_entry(hass, data.get(ATTR_CONFIG_ENTRY))
    model = data.get(CONF_MODEL)
    provider = entry.runtime_data.provider
    if model is not None and not model_offered(provider, model):
        raise _invalid("model_not_offered", model=model, provider=provider.title)
    return _Call(
        entry=entry,
        caller=await _caller(hass, call),
        target={key: data[key] for key in TARGET_KEYS if data.get(key)},
        note=data.get(CONF_NOTE),
        pictures=tuple(sources),
        model=model,
        with_attributes=data.get(CONF_WITH_ATTRIBUTES, False),
        window=data.get(CONF_WINDOW),
    )


async def _fetch(hass: HomeAssistant, request: _Call) -> Pictures:
    """The call's pictures, once the budget has room for at least one request."""
    check_room(request.entry)
    return await async_request_images(
        hass,
        request.pictures,
        request.entry.runtime_data.client.dialect,
        max_images=max_images(request.entry),
    )


async def _send(
    hass: HomeAssistant, request: _Call, questions: Mapping[str, Question]
) -> JevResponse:
    """Check what the call reads, build its state and ask about it."""
    if not (request.target or request.note or request.pictures):
        raise _invalid("nothing_to_read")
    if request.window is not None and not request.target:
        raise _invalid("window_without_target")
    _check_caller_may_read(hass, request.caller, request.pictures, request.target)
    pictures = await _fetch(hass, request) if request.pictures else None
    # Taken as written. A script or an automation renders its data before the
    # call, so a template here was rendered once already, and its text can hold
    # whatever an MQTT payload or a webhook put there. Rendering it again would
    # run that text as a template that may read any entity.
    note = request.note
    if request.window is None:
        built = async_state(hass, note, request.target, with_attributes=request.with_attributes)
    else:
        # Read from the recorder for this call alone and dropped after it.
        built = await async_window_state_once(
            hass, note, request.target, request.with_attributes, window_from_mapping(request.window)
        )
    state = attach(built, pictures)
    _LOGGER.debug("Sending %d question(s) about %s", len(questions), state)
    return await async_ask(
        hass, request.entry, state, questions, pictures=pictures, model=request.model
    )


def _receipt(response: JevResponse) -> dict[str, Any]:
    """What every action reply ends with: who answered, how fast, at what cost."""
    return {
        "model": response.model,
        ATTR_LATENCY_MS: round(response.latency_ms, 1),
        ATTR_USAGE: asdict(response.usage),
    }


_KINDS: dict[type[Answer], str] = {
    NoulAnswer: TYPE_NOUL,
    ChoiceAnswer: TYPE_CHOICE,
    ScoreAnswer: TYPE_SCORE,
    BoxAnswer: TYPE_BOX,
}


def _sole[A: (NoulAnswer, ChoiceAnswer, ScoreAnswer, BoxAnswer)](
    response: JevResponse, expected: type[A]
) -> tuple[A, dict[str, Any]]:
    """A one-question reply's answer, and its fields without the type.

    A server that answers some other type answered a different question, and
    its numbers mean nothing for this one.
    """
    answer = response.answers.get(ANSWER_KEY)
    if not isinstance(answer, expected):
        raise _failed("answer_mismatch", question_type=_KINDS[expected], got=type(answer).__name__)
    fields = answer_as_dict(answer)
    del fields[CONF_TYPE]
    return answer, fields


def _within(count: int, low: int, high: int, key: str) -> None:
    if not low <= count <= high:
        raise _invalid(key, min=str(low), max=str(high), count=str(count))


async def _ask_one(hass: HomeAssistant, call: ServiceCall, kind: str) -> JevResponse:
    question = question_from_mapping({**call.data, CONF_TYPE: kind})
    return await _send(hass, await _read_call(hass, call), {ANSWER_KEY: question})


async def _noul(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    response = await _ask_one(hass, call, TYPE_NOUL)
    answer, fields = _sole(response, NoulAnswer)
    cutoff: float = call.data[CONF_CUTOFF]
    # Near 0.5 the model cannot tell. That is no half-truth, so the verdict is
    # only ever the caller's own cutoff applied to the number.
    return fields | {"verdict": answer.noul >= cutoff, "cutoff": cutoff} | _receipt(response)


def _floor_field(floor: Any, answer: Answer) -> dict[str, bool]:
    """Whether the answer cleared the caller's floor, beside the answer and never for it.

    A script reads the model's own word next to this, and decides what to do.
    """
    sure = confident(floor, answer)
    return {} if sure is None else {ATTR_CONFIDENT: sure}


async def _choice(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    _within(len(call.data[CONF_OPTIONS]), MIN_CHOICE_OPTIONS, MAX_CHOICE_OPTIONS, "options_count")
    floor = call.data.get(CONF_MIN_CONFIDENCE)
    if problem := floor_problem(TYPE_CHOICE, call.data[CONF_OPTIONS], floor, None):
        raise _invalid("min_confidence_invalid", reason=problem)
    response = await _ask_one(hass, call, TYPE_CHOICE)
    answer, fields = _sole(response, ChoiceAnswer)
    return fields | _floor_field(floor, answer) | _receipt(response)


async def _score(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    _within(len(call.data[CONF_SCALE]), MIN_SCORE_LEVELS, MAX_SCORE_LEVELS, "scale_count")
    response = await _ask_one(hass, call, TYPE_SCORE)
    answer, fields = _sole(response, ScoreAnswer)
    return fields | _floor_field(call.data.get(CONF_MIN_CONFIDENCE), answer) | _receipt(response)


def _listed_question(key: str, raw: Mapping[str, Any]) -> Question:
    """One question of a mentat.ask call, refused against the caller's key."""
    if not {CONF_TYPE, CONF_INSTRUCTIONS} <= raw.keys():
        raise _invalid("question_shape", key=key)
    try:
        return question_from_mapping(raw)
    except (KeyError, ValueError, TypeError) as err:
        raise _invalid("question_invalid", key=key, reason=str(err)) from err


def _listed_cutoff(key: str, raw: Mapping[str, Any]) -> float | None:
    """A noul's cutoff in a mentat.ask call, or None when it has none.

    A release holds a verdict from one evaluation of a watch to the next, and
    an action keeps nothing between calls, so it is refused rather than ignored.
    """
    if raw.get(CONF_RELEASE) is not None:
        raise _invalid("question_release", key=key)
    if raw.get(CONF_CUTOFF) is None:
        return None
    if raw[CONF_TYPE] != TYPE_NOUL:
        raise _invalid("question_cutoff_noul_only", key=key)
    try:
        cutoff: float = PROBABILITY(raw[CONF_CUTOFF])
    except vol.Invalid as err:
        raise _invalid("question_invalid", key=key, reason=f"cutoff: {err}") from err
    return cutoff


def _listed_floor(key: str, raw: Mapping[str, Any], question: Question) -> Any:
    """A choice or score's min_confidence in a mentat.ask call, or None.

    A fallback is refused rather than ignored: a response never swaps the model's
    word for another, so the script that reads `confident` picks its own.
    """
    if raw.get(CONF_FALLBACK) is not None:
        raise _invalid("question_fallback", key=key)
    if raw.get(CONF_MIN_CONFIDENCE) is None:
        return None
    if raw[CONF_TYPE] not in (TYPE_CHOICE, TYPE_SCORE):
        reason = "only a choice or a score carries a confidence"
        raise _invalid("question_invalid", key=key, reason=reason)
    try:
        floor = MIN_CONFIDENCE(raw[CONF_MIN_CONFIDENCE])
    except vol.Invalid as err:
        raise _invalid("question_invalid", key=key, reason=f"min_confidence: {err}") from err
    options = question.criteria if isinstance(question, Choice) else ()
    if problem := floor_problem(raw[CONF_TYPE], options, floor, None):
        raise _invalid("question_invalid", key=key, reason=problem)
    return floor


async def _ask(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    listed: Mapping[str, Mapping[str, Any]] = call.data[ATTR_QUESTIONS]
    questions = {key: _listed_question(key, raw) for key, raw in listed.items()}
    cutoffs = {key: _listed_cutoff(key, raw) for key, raw in listed.items()}
    floors = {key: _listed_floor(key, raw, questions[key]) for key, raw in listed.items()}
    if not call.data[CONF_PICTURES]:
        for key, question in questions.items():
            if isinstance(question, Box):
                raise _invalid("box_needs_image", key=key)
    response = await _send(hass, await _read_call(hass, call), questions)
    answers: dict[str, Any] = {}
    for key, answer in response.answers.items():
        answers[key] = answer_as_dict(answer) | _floor_field(floors.get(key), answer)
        cutoff = cutoffs.get(key)
        if cutoff is not None and isinstance(answer, NoulAnswer):
            answers[key] |= {"verdict": answer.noul >= cutoff, "cutoff": cutoff}
    return {ATTR_ANSWERS: answers} | _receipt(response)


async def _box(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    request = await _read_call(hass, call)
    _check_caller_may_read(hass, request.caller, request.pictures)
    # A Clef entry fetches the picture too. Its client then refuses the box
    # before posting, async_ask reports box_refused, and nothing is billed.
    pictures = await _fetch(hass, request)
    question = question_from_mapping({**call.data, CONF_TYPE: TYPE_BOX})
    response = await async_ask(
        hass,
        request.entry,
        attach(None, pictures),
        {ANSWER_KEY: question},
        pictures=pictures,
        model=request.model,
    )
    # The upright size the source had: an edge in pixels is its fraction times this.
    width, height = pictures.ordered[0].source
    edges = _sole(response, BoxAnswer)[1]
    return edges | {"width": width, "height": height} | _receipt(response)


async def _calibrate(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """The cutoff and release that replay a noul's history best. Sends nothing.

    A history says as much about an entity as its state does, so a user who may
    not read either entity is refused before the recorder is read.
    """
    sensor: str = call.data[CONF_SENSOR]
    truth: str = call.data[CONF_TRUTH]
    truth_state: str = call.data[CONF_TRUTH_STATE]
    days: int = call.data[CONF_DAYS]
    _check_caller_may_read(hass, await _caller(hass, call), (), entities=(sensor, truth))
    if "recorder" not in hass.config.components:
        raise _invalid("calibrate_needs_recorder")
    end = dt_util.utcnow()
    start = end - timedelta(days=days)
    try:
        found = await async_recorded_states(hass, start, end, [sensor, truth])
    except RecorderUnreadableError as err:
        # A database error quotes its SQL, which is for the log and not for a caller.
        _LOGGER.warning("mentat.calibrate could not read the recorder: %s", err)
        raise _failed("calibrate_recorder_failed") from err
    said = found[sensor]
    if len(said) + len(found[truth]) > MAX_CALIBRATE_CHANGES:
        raise _invalid(
            "calibrate_too_long", sensor=sensor, truth=truth, limit=str(MAX_CALIBRATE_CHANGES)
        )
    probabilities = [
        value for value in map(number, (state.state for state in said)) if value is not None
    ]
    placeholders = {"sensor": sensor, "truth": truth, "state": truth_state, "days": str(days)}
    # A score or a count would give cutoffs that mean nothing.
    if any(not 0 <= value <= 1 for value in probabilities):
        raise _invalid("calibrate_not_probability", **placeholders)
    grace = timedelta(seconds=call.data[CONF_GRACE])
    pieces = pieces_of(said, found[truth], truth_state, start, end, grace)
    if not pieces:
        raise _invalid("calibrate_no_history", **placeholders)
    # With one side only there is nothing to separate, and every number scores alike.
    if not any(piece.truth for piece in pieces):
        raise _invalid("calibrate_never_true", **placeholders)
    if all(piece.truth for piece in pieces):
        raise _invalid("calibrate_always_true", **placeholders)
    # A pass over the history per pair of numbers is too long for the event loop.
    return await hass.async_add_executor_job(calibration, pieces, len(probabilities))


type _Handler = Callable[[HomeAssistant, ServiceCall], Coroutine[Any, Any, ServiceResponse]]

_ACTIONS: dict[str, tuple[_Handler, vol.Schema]] = {
    SERVICE_NOUL: (_noul, NOUL_SCHEMA),
    SERVICE_CHOICE: (_choice, CHOICE_SCHEMA),
    SERVICE_SCORE: (_score, SCORE_SCHEMA),
    SERVICE_ASK: (_ask, ASK_SCHEMA),
    SERVICE_BOX: (_box, BOX_SCHEMA),
    SERVICE_CALIBRATE: (_calibrate, CALIBRATE_SCHEMA),
}


def async_register_services(hass: HomeAssistant) -> None:
    """Register the actions the first time an entry loads; later loads find them."""
    if hass.services.has_service(DOMAIN, SERVICE_NOUL):
        return
    for name, (handler, schema) in _ACTIONS.items():
        hass.services.async_register(
            DOMAIN,
            name,
            partial(handler, hass),
            schema=schema,
            supports_response=SupportsResponse.ONLY,
        )
