"""The YAML schema: refuse at config time what the API would refuse later.

A mistake caught here costs nothing. The same mistake sent to the API costs a
request and comes back as a 422 about a field the user never typed, so every
message names the key as the user wrote it, and the watch or question it is in.
Out come validated dicts with their defaults filled in; watches.py builds from
them. Nothing here builds a question, reads state or talks to a server.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import timedelta
from typing import Any, Final, cast

import voluptuous as vol
from homeassistant.const import CONF_NAME, CONF_SCAN_INTERVAL
from homeassistant.helpers import config_validation as cv
from homeassistant.util import slugify
from jevclient import (
    MAX_CHOICE_OPTIONS,
    MAX_SCORE_LEVELS,
    MIN_CHOICE_OPTIONS,
    MIN_SCORE_LEVELS,
)

from .const import (
    CONF_BRIEF,
    CONF_COLLAPSE_REPEATS,
    CONF_CRITERIA,
    CONF_CUTOFF,
    CONF_DURATION,
    CONF_ENTITIES,
    CONF_EVALUATE,
    CONF_EVENT_DATA,
    CONF_EVENT_TYPE,
    CONF_EVENTS,
    CONF_FIRE_EVENT,
    CONF_HIGH,
    CONF_INSTRUCTIONS,
    CONF_LABEL,
    CONF_LOW,
    CONF_MAX_EVENTS,
    CONF_NOTE,
    CONF_QUESTIONS,
    CONF_SETTLE,
    CONF_TYPE,
    CONF_WAKE_ON,
    CONF_WINDOW,
    CONF_WITH_ATTRIBUTES,
    DEFAULT_WINDOW_EVENTS,
    DEFAULT_WINDOW_SETTLE_SECONDS,
    DOMAIN,
    EVALUATE_BOTH,
    EVALUATE_INTERVAL,
    EVALUATE_MODES,
    MAX_WINDOW_EVENTS,
    MAX_WINDOW_SECONDS,
    MAX_WINDOW_SETTLE_SECONDS,
    MIN_SCAN_INTERVAL_SECONDS,
    MIN_WINDOW_EVENTS,
    MIN_WINDOW_SECONDS,
    TYPE_CHOICE,
    TYPE_NOUL,
    TYPE_SCORE,
)
from .watches import is_own_event, yaml_key

# Anywhere the API takes text it also takes an object or a list. The model reads
# structure, so a rubric can go in as keys rather than one long sentence.
ENTRY = vol.Any(cv.string, dict, list)

# YAML 1.1 reads an unquoted yes, no, on or off as a boolean, and cv.string would
# then send "True" as what a noul's high end means.
UNQUOTED_BOOLEAN: Final = (
    "an unquoted yes, no, on or off reads as true or false in YAML; put the word in quotes"
)


def not_boolean(value: Any) -> Any:
    """Refuse a boolean where a word belongs, and say to quote it."""
    if isinstance(value, bool):
        raise vol.Invalid(UNQUOTED_BOOLEAN)
    return value


# A word or a structure, never a bare boolean: high, low and every scale level.
WORDING = vol.All(not_boolean, ENTRY)

_RENAME: Final = "give them names that differ by more than punctuation or case"
_NOUL_ONLY: Final = "'high:', 'low:' and 'cutoff:' apply to a noul only"

# What a question of the wrong type says for each key it should not carry.
_STRAY: Final = {
    CONF_CRITERIA: "a noul takes 'high:' and 'low:', not 'criteria:'",
    CONF_HIGH: _NOUL_ONLY,
    CONF_LOW: _NOUL_ONLY,
    CONF_CUTOFF: _NOUL_ONLY,
}


def _options_fit(criteria: Any) -> str | None:
    if isinstance(criteria, dict) and MIN_CHOICE_OPTIONS <= len(criteria) <= MAX_CHOICE_OPTIONS:
        return None
    return (
        f"a choice needs 'criteria:' as a mapping of {MIN_CHOICE_OPTIONS} to "
        f"{MAX_CHOICE_OPTIONS} options"
    )


def _levels_fit(criteria: Any) -> str | None:
    if isinstance(criteria, list) and MIN_SCORE_LEVELS <= len(criteria) <= MAX_SCORE_LEVELS:
        return None
    return (
        f"a score needs 'criteria:' as a list of {MIN_SCORE_LEVELS} to "
        f"{MAX_SCORE_LEVELS} levels, lowest first"
    )


# Per type: which of the typed keys it may carry, and what its criteria must be.
_SHAPES: Final[dict[str, tuple[frozenset[str], Callable[[Any], str | None] | None]]] = {
    TYPE_NOUL: (frozenset({CONF_HIGH, CONF_LOW, CONF_CUTOFF}), None),
    TYPE_CHOICE: (frozenset({CONF_CRITERIA}), _options_fit),
    TYPE_SCORE: (frozenset({CONF_CRITERIA}), _levels_fit),
}


def question_shape(raw: dict[str, Any]) -> dict[str, Any]:
    """Refuse criteria of the wrong shape, then any key the type does not take."""
    allowed, check = _SHAPES[raw[CONF_TYPE]]
    problem = check(raw.get(CONF_CRITERIA)) if check is not None else None
    if problem is None:
        stray = [key for key in _STRAY if key not in allowed and raw.get(key) is not None]
        problem = _STRAY[stray[0]] if stray else None
    if problem is not None:
        raise vol.Invalid(f"question {raw.get(CONF_NAME, '?')!r}: {problem}")
    return raw


QUESTION_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required(CONF_NAME): cv.string,
            vol.Required(CONF_TYPE): vol.In(list(_SHAPES)),
            vol.Required(CONF_INSTRUCTIONS): ENTRY,
            vol.Optional(CONF_BRIEF): ENTRY,
            vol.Optional(CONF_HIGH): WORDING,
            vol.Optional(CONF_LOW): WORDING,
            vol.Optional(CONF_CRITERIA): vol.Any({cv.string: vol.Any(ENTRY, None)}, [WORDING]),
            vol.Optional(CONF_CUTOFF): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0)),
        }
    ),
    question_shape,
)


# A watch fires mentat_decision after every answer. A window that logged it
# would settle after each answer and ask again, as fast as the model replies,
# until the budget ran out.
_LOOPS: Final = "Mentat fires it after every answer, so the watch would ask again after each one"


def _not_own_event(event_type: str) -> str:
    """An event type a window may log: anything Mentat does not fire itself."""
    if is_own_event(event_type):
        raise vol.Invalid(f"a window cannot log {event_type!r}: {_LOOPS}")
    return event_type


EVENT_FILTER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EVENT_TYPE): vol.All(cv.string, _not_own_event),
        vol.Optional(CONF_EVENT_DATA, default=dict): dict,
        vol.Optional(CONF_LABEL): cv.string,
    }
)


_SHORTEST: Final = timedelta(seconds=MIN_WINDOW_SECONDS)
_LONGEST: Final = timedelta(seconds=MAX_WINDOW_SECONDS)


def window_shape(raw: dict[str, Any]) -> dict[str, Any]:
    """A window lasts a minute to a day and settles within its own length."""
    duration: timedelta = raw[CONF_DURATION]
    problem = None
    if not _SHORTEST <= duration <= _LONGEST:
        problem = (
            f"a window lasts from {MIN_WINDOW_SECONDS // 60} minute to "
            f"{MAX_WINDOW_SECONDS // 3600} hours, got {duration}"
        )
    elif timedelta(seconds=raw[CONF_SETTLE]) > duration:
        problem = f"settle ({raw[CONF_SETTLE]:g} s) cannot be longer than the window ({duration})"
    if problem is not None:
        raise vol.Invalid(problem)
    return raw


WINDOW_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required(CONF_DURATION): cv.positive_time_period,
            vol.Optional(CONF_MAX_EVENTS, default=DEFAULT_WINDOW_EVENTS): vol.All(
                vol.Coerce(int), vol.Range(min=MIN_WINDOW_EVENTS, max=MAX_WINDOW_EVENTS)
            ),
            vol.Optional(CONF_SETTLE, default=DEFAULT_WINDOW_SETTLE_SECONDS): vol.All(
                vol.Coerce(float), vol.Range(min=0, max=MAX_WINDOW_SETTLE_SECONDS)
            ),
            vol.Optional(CONF_EVALUATE, default=EVALUATE_BOTH): vol.In(EVALUATE_MODES),
            vol.Optional(CONF_EVENTS, default=list): vol.All(cv.ensure_list, [EVENT_FILTER_SCHEMA]),
            vol.Optional(CONF_WITH_ATTRIBUTES, default=False): cv.boolean,
            vol.Optional(CONF_COLLAPSE_REPEATS, default=True): cv.boolean,
        }
    ),
    window_shape,
)

TARGET_SCHEMA = vol.Schema(
    {
        vol.Optional("entity_id"): cv.entity_ids,
        vol.Optional("device_id"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("area_id"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("floor_id"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("label_id"): vol.All(cv.ensure_list, [cv.string]),
    }
)


def _as_target(value: Any) -> dict[str, Any]:
    """`entities:` as a target. A bare list of entity ids is the short form."""
    written = value if isinstance(value, dict) else {"entity_id": value}
    return cast(dict[str, Any], TARGET_SCHEMA(written))


def _first_repeat[T](pairs: Iterable[tuple[str, T]]) -> tuple[str, T, T] | None:
    """The first key met twice, with what carried it the first and second time.

    Names become keys through slugify, so "House mode" and "house-mode" are one
    key though they read as two.
    """
    seen: dict[str, T] = {}
    for key, label in pairs:
        if key in seen:
            return key, seen[key], label
        seen[key] = label
    return None


def watch_shape(raw: dict[str, Any]) -> dict[str, Any]:
    """A watch reads something, keys each question once, and can run its window."""
    name = raw.get(CONF_NAME, "?")
    if not raw.get(CONF_NOTE) and not raw.get(CONF_ENTITIES):
        raise vol.Invalid(
            f"watch {name!r}: give it 'entities:' to look at, 'note:' to write "
            "the text yourself, or both"
        )
    # Question keys are slugs. Two alike would ask one question and drop the
    # other's entity.
    names = (question[CONF_NAME] for question in raw[CONF_QUESTIONS])
    if repeat := _first_repeat((slugify(each), each) for each in names):
        key, first, second = repeat
        raise vol.Invalid(
            f"watch {name!r}: questions {first!r} and {second!r} both become the key {key!r}; "
            + _RENAME
        )
    window = raw.get(CONF_WINDOW)
    if window is None:
        return raw
    if not raw.get(CONF_ENTITIES):
        raise vol.Invalid(
            f"watch {name!r}: a window logs changes to entities, so it needs 'entities:'"
        )
    if window[CONF_EVALUATE] == EVALUATE_INTERVAL and CONF_SCAN_INTERVAL not in raw:
        raise vol.Invalid(
            f"watch {name!r}: 'evaluate: interval' judges the window on a "
            "schedule, so it needs 'scan_interval:'"
        )
    return raw


WATCH_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required(CONF_NAME): cv.string,
            vol.Optional(CONF_NOTE): cv.template,
            vol.Optional(CONF_ENTITIES): _as_target,
            vol.Optional(CONF_WITH_ATTRIBUTES, default=False): cv.boolean,
            # No default, so a window can tell an interval that was written from
            # one that was not. watch_from_yaml() fills it in.
            vol.Optional(CONF_SCAN_INTERVAL): vol.All(
                vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL_SECONDS)
            ),
            vol.Optional(CONF_WAKE_ON, default=list): cv.entity_ids,
            vol.Optional(CONF_WINDOW): WINDOW_SCHEMA,
            vol.Optional(CONF_FIRE_EVENT, default=True): cv.boolean,
            vol.Required(CONF_QUESTIONS): vol.All(
                cv.ensure_list, [QUESTION_SCHEMA], vol.Length(min=1)
            ),
        }
    ),
    watch_shape,
)


def unique_keys(watches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Refuse two watches with one key, or two questions with one key.

    Two watches with one key would replace each other's coordinator. Two
    questions with one key would give two entities one unique id. A question key
    joins two slugs with an underscore, so "House mode" asking "Quiet" and
    "House" asking "Mode quiet" collide though neither watch repeats itself.
    """
    if repeat := _first_repeat((slugify(w[CONF_NAME]), w[CONF_NAME]) for w in watches):
        key, first, second = repeat
        raise vol.Invalid(
            f"watches {first!r} and {second!r} both become the key {key!r}; " + _RENAME
        )
    asked = (
        (yaml_key(watch[CONF_NAME], question[CONF_NAME]), (watch[CONF_NAME], question[CONF_NAME]))
        for watch in watches
        for question in watch[CONF_QUESTIONS]
    )
    if clash := _first_repeat(asked):
        key, (watch_a, question_a), (watch_b, question_b) = clash
        raise vol.Invalid(
            f"question {question_a!r} in watch {watch_a!r} and question "
            f"{question_b!r} in watch {watch_b!r} both become the key {key!r}; "
            "rename one of them"
        )
    return watches


CONFIG_SCHEMA = vol.Schema(
    {DOMAIN: vol.All(cv.ensure_list, [WATCH_SCHEMA], unique_keys)},
    extra=vol.ALLOW_EXTRA,
)
