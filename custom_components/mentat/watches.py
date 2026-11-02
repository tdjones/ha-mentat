"""Watches: one request each, and the questions it carries.

A watch is a target and/or a note, the questions judged against it, how often and
on what it wakes, and an optional window. It comes from a YAML block that
schema.py has already checked, or from UI questions grouped by what they read and
when. Grouping is where the money is saved. Questions in one watch share one state
and one request, so a second question costs its own text and nothing more, while
a second watch costs a whole call.

This module builds and never validates: a mapping that reaches it has passed
schema.py, an action schema or the editor's form. It reads no state and sends
nothing. The dataclasses are frozen with tuple fields, so a coordinator can hold
one for the life of the entry without anyone changing it underneath.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from hashlib import sha256
from typing import Any, Final, Literal, cast

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import CONF_NAME, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.template import Template
from homeassistant.util import slugify
from jevclient import (
    Answer,
    Box,
    BoxAnswer,
    Choice,
    ChoiceAnswer,
    Noul,
    NoulAnswer,
    Question,
    Score,
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
    CONF_OPTIONS,
    CONF_QUESTIONS,
    CONF_SCALE,
    CONF_SETTLE,
    CONF_TARGET,
    CONF_TYPE,
    CONF_WAKE_ON,
    CONF_WINDOW,
    CONF_WINDOW_COLLAPSE_REPEATS,
    CONF_WINDOW_DURATION,
    CONF_WINDOW_ENABLED,
    CONF_WINDOW_EVALUATE,
    CONF_WINDOW_EVENTS,
    CONF_WINDOW_MAX_EVENTS,
    CONF_WINDOW_SETTLE,
    CONF_WITH_ATTRIBUTES,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DEFAULT_WINDOW_EVENTS,
    DEFAULT_WINDOW_SETTLE_SECONDS,
    EVALUATE_BOTH,
    OWN_EVENT_PREFIX,
    SUBENTRY_QUESTION,
    TYPE_BOX,
    TYPE_CHOICE,
    TYPE_NOUL,
    TYPE_SCORE,
)

type Evaluate = Literal["on_event", "interval", "both"]

# The edges of a box answer and its confidence, in the order they are reported.
BOX_SIDES: Final = ("left", "top", "right", "bottom", "confidence")


@dataclass(slots=True, frozen=True)
class EventFilter:
    """A bus event a window logs: its type, and data the event must carry."""

    event_type: str
    data: Mapping[str, Any] = field(default_factory=dict)
    label: str | None = None


@dataclass(slots=True, frozen=True)
class WindowConfig:
    """How far back a window looks, what it logs, and when it is judged."""

    duration: timedelta
    max_events: int = DEFAULT_WINDOW_EVENTS
    settle: float = DEFAULT_WINDOW_SETTLE_SECONDS
    evaluate: Evaluate = "both"
    events: tuple[EventFilter, ...] = ()
    with_attributes: bool = False
    collapse_repeats: bool = True


@dataclass(slots=True, frozen=True)
class QuestionSpec:
    """One question and the settings of the entities it gets."""

    key: str
    name: str
    kind: str
    question: Question
    cutoff: float | None = None

    @property
    def wants_verdict(self) -> bool:
        """A noul with a cutoff gets a verdict binary sensor next to its sensor."""
        return self.cutoff is not None and isinstance(self.question, Noul)

    @property
    def choices(self) -> tuple[str, ...]:
        """The states a choice's sensor can take. Other types have none."""
        question = self.question
        return tuple(question.criteria) if isinstance(question, Choice) else ()


@dataclass(slots=True, frozen=True)
class WatchConfig:
    """One request: what it reads, when it wakes, and every question it asks."""

    key: str
    name: str
    questions: tuple[QuestionSpec, ...]
    scan_interval: int
    note: Template | None = None
    target: Mapping[str, Any] | None = None
    with_attributes: bool = False
    wake_on: tuple[str, ...] = ()
    window: WindowConfig | None = None
    fire_event: bool = True
    # A YAML watch's entities are keyed apart from any entry, because the entry
    # that runs the YAML watches can change.
    from_yaml: bool = False


def is_own_event(event_type: Any) -> bool:
    """Whether Mentat fires this event type itself, as it does mentat_decision."""
    return str(event_type).startswith(OWN_EVENT_PREFIX)


def fold_brief(instructions: Any, brief: Any) -> Any:
    """Put a question's standing facts into its instructions, not into the state.

    The model weighs a rule written into the question more than the same rule
    left in the readings for it to find. Nobody has measured that yet, and
    docs/measurements.md lists it as an open figure. A mapping brief merges into the
    instructions key by key, so the author's key names reach the model. A text
    brief goes under "background". The field is called brief everywhere else, but
    the model reads that key, so the key keeps its old name.
    """
    if not brief:
        return instructions
    folded = dict(instructions) if isinstance(instructions, Mapping) else {"question": instructions}
    folded.update(brief if isinstance(brief, Mapping) else {"background": brief})
    return folded


def options_from(value: Any) -> dict[str, Any]:
    """A choice's options as name to description, with None for no description.

    An action writes a list of names or a mapping. The editor writes one option
    per line, `name: description`, and the description may be left off.
    """
    if isinstance(value, str):
        rows = (line.partition(":") for line in value.splitlines() if line.strip())
        return {name.strip(): about.strip() or None for name, _, about in rows}
    if isinstance(value, Mapping):
        return {name: about or None for name, about in value.items()}
    return dict.fromkeys(value)


def scale_from(value: Any) -> list[Any]:
    """A score's levels, lowest first. The editor writes one level per line."""
    if not isinstance(value, str):
        return list(value)
    return [level for level in map(str.strip, value.splitlines()) if level]


def _make_noul(instructions: Any, raw: Mapping[str, Any]) -> Question:
    # Blank is absent: a blueprint passes "" for a field its user left empty.
    return Noul(instructions, true=raw.get(CONF_HIGH) or None, false=raw.get(CONF_LOW) or None)


def _make_choice(instructions: Any, raw: Mapping[str, Any]) -> Question:
    if CONF_CRITERIA in raw:
        return Choice(instructions, raw[CONF_CRITERIA])
    return Choice(instructions, options_from(raw[CONF_OPTIONS]))


def _make_score(instructions: Any, raw: Mapping[str, Any]) -> Question:
    if CONF_CRITERIA in raw:
        return Score(instructions, raw[CONF_CRITERIA])
    return Score(instructions, scale_from(raw[CONF_SCALE]))


def _make_box(instructions: Any, raw: Mapping[str, Any]) -> Question:
    return Box(instructions)


_MAKERS: Final[dict[str, Callable[[Any, Mapping[str, Any]], Question]]] = {
    TYPE_NOUL: _make_noul,
    TYPE_CHOICE: _make_choice,
    TYPE_SCORE: _make_score,
    TYPE_BOX: _make_box,
}


def question_from_mapping(raw: Mapping[str, Any]) -> Question:
    """One question from YAML, an action or the editor.

    All three write `type`, `instructions` and `brief`, and a noul's `high` and
    `low`. YAML and `mentat.ask` give a choice or a score its `criteria` as the
    API takes them. The choice and score actions and the editor say `options` and
    `scale` instead, in the forms options_from() and scale_from() read. A choice
    with neither raises KeyError, and an unknown type raises ValueError, so the
    ask action can report both against the question's key.
    """
    make = _MAKERS.get(raw[CONF_TYPE])
    if make is None:
        raise ValueError(f"unknown question type {raw[CONF_TYPE]!r}")
    return make(fold_brief(raw[CONF_INSTRUCTIONS], raw.get(CONF_BRIEF)), raw)


def cutoff_from(raw: Mapping[str, Any]) -> float | None:
    """The cutoff as a number, or None when there is none.

    The editor stores a cleared number box as "", which means no cutoff.
    """
    cutoff = raw.get(CONF_CUTOFF)
    return None if cutoff in (None, "") else float(cutoff)


def question_spec(raw: Mapping[str, Any], key: str) -> QuestionSpec:
    """A named question under the key its entities' unique ids are made from."""
    return QuestionSpec(
        key=key,
        name=raw[CONF_NAME],
        kind=raw[CONF_TYPE],
        question=question_from_mapping(raw),
        cutoff=cutoff_from(raw),
    )


def _event_filter(raw: Mapping[str, Any]) -> EventFilter:
    return EventFilter(
        event_type=raw[CONF_EVENT_TYPE],
        data=dict(raw.get(CONF_EVENT_DATA) or {}),
        label=raw.get(CONF_LABEL),
    )


def window_from_mapping(raw: Mapping[str, Any]) -> WindowConfig:
    """A window from what WINDOW_SCHEMA returns. A key left out takes its default."""
    return WindowConfig(
        duration=raw[CONF_DURATION],
        max_events=int(raw.get(CONF_MAX_EVENTS, DEFAULT_WINDOW_EVENTS)),
        settle=float(raw.get(CONF_SETTLE, DEFAULT_WINDOW_SETTLE_SECONDS)),
        evaluate=cast(Evaluate, raw.get(CONF_EVALUATE, EVALUATE_BOTH)),
        events=tuple(map(_event_filter, raw.get(CONF_EVENTS) or ())),
        with_attributes=bool(raw.get(CONF_WITH_ATTRIBUTES)),
        collapse_repeats=bool(raw.get(CONF_COLLAPSE_REPEATS, True)),
    )


# The editor's window fields, under the YAML key each one stands for. A UI
# window has no attribute switch of its own and follows its question's.
_EDITOR_WINDOW: Final = {
    CONF_MAX_EVENTS: CONF_WINDOW_MAX_EVENTS,
    CONF_SETTLE: CONF_WINDOW_SETTLE,
    CONF_EVALUATE: CONF_WINDOW_EVALUATE,
    CONF_EVENTS: CONF_WINDOW_EVENTS,
    CONF_COLLAPSE_REPEATS: CONF_WINDOW_COLLAPSE_REPEATS,
    CONF_WITH_ATTRIBUTES: CONF_WITH_ATTRIBUTES,
}


def _editor_window(data: Mapping[str, Any]) -> WindowConfig | None:
    """The window a UI question keeps, or None while its switch is off."""
    if not data.get(CONF_WINDOW_ENABLED):
        return None
    raw = {key: data[ui] for key, ui in _EDITOR_WINDOW.items() if ui in data}
    raw[CONF_DURATION] = cv.time_period_dict(data[CONF_WINDOW_DURATION])
    return window_from_mapping(raw)


def _tidy_target(target: Any) -> dict[str, list[str]]:
    """A picked target without its empty kinds and with its ids sorted.

    The picker leaves {"area_id": []} behind once something is picked and
    removed. Two targets that pick the same things then compare equal.
    """
    if not isinstance(target, Mapping):
        return {}
    return {kind: sorted(cv.ensure_list(ids)) for kind, ids in sorted(target.items()) if ids}


def yaml_key(watch: str, question: str) -> str:
    """A YAML question's key, which its entities' unique ids start with."""
    return "_".join((slugify(watch), slugify(question)))


def watch_from_yaml(hass: HomeAssistant, raw: Mapping[str, Any]) -> WatchConfig:
    """A watch from one YAML block that WATCH_SCHEMA has passed.

    The watch key is its name's slug. The schema leaves scan_interval out when the
    user did, so a window can tell; the default goes in here.
    """
    name: str = raw[CONF_NAME]
    window = raw.get(CONF_WINDOW)
    written: Template | None = raw.get(CONF_NOTE)
    return WatchConfig(
        key=slugify(name),
        name=name,
        questions=tuple(
            question_spec(item, yaml_key(name, item[CONF_NAME])) for item in raw[CONF_QUESTIONS]
        ),
        scan_interval=raw.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_SECONDS),
        note=None if written is None else Template(written.template, hass),
        target=raw.get(CONF_ENTITIES),
        with_attributes=raw.get(CONF_WITH_ATTRIBUTES, False),
        wake_on=tuple(raw.get(CONF_WAKE_ON, ())),
        window=None if window is None else window_from_mapping(window),
        fire_event=raw.get(CONF_FIRE_EVENT, True),
        from_yaml=True,
    )


def share_key(data: Mapping[str, Any]) -> str:
    """What two UI questions must agree on to go in one request.

    The API takes one state per request. Two questions share it when they read
    the same target and note, wake on the same entities on the same schedule, and
    keep the same window. The settings of a window that is switched off count for
    nothing, so leaving them filled in splits no group.
    """
    window = _editor_window(data)
    shared = {
        "target": _tidy_target(data.get(CONF_TARGET)),
        "note": data.get(CONF_NOTE) or "",
        "with_attributes": bool(data.get(CONF_WITH_ATTRIBUTES)),
        "wake_on": sorted(data.get(CONF_WAKE_ON) or ()),
        "scan_interval": int(data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_SECONDS)),
        "window": None if window is None else asdict(window),
    }
    return json.dumps(shared, sort_keys=True, default=str)


def watch_from_question(
    hass: HomeAssistant,
    data: Mapping[str, Any],
    *,
    key: str = "draft",
    name: str = "draft",
    questions: Sequence[QuestionSpec] = (),
) -> WatchConfig:
    """A watch that reads what one UI question reads.

    The grouping passes the group's key, name and questions, and every member
    agrees on what is read here, since all of it is in the share key. The editor's
    preview passes a question that is not saved yet and leaves the rest out.
    """
    note = data.get(CONF_NOTE)
    return WatchConfig(
        key=key,
        name=name,
        questions=tuple(questions),
        scan_interval=int(data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_SECONDS)),
        note=Template(note, hass) if note else None,
        target=_tidy_target(data.get(CONF_TARGET)) or None,
        with_attributes=bool(data.get(CONF_WITH_ATTRIBUTES)),
        wake_on=tuple(data.get(CONF_WAKE_ON) or ()),
        window=_editor_window(data),
    )


def _ui_key(subentry: ConfigSubentry) -> str:
    """The subentry id alone, so a renamed question keeps its entities."""
    return f"ui_{subentry.subentry_id.lower()}"


def watches_from_subentries(hass: HomeAssistant, entry: ConfigEntry) -> list[WatchConfig]:
    """The entry's UI questions in as few watches as their settings allow.

    A watch of one question is named after it, so a log line says something its
    reader knows. A larger one is "Group N", counted in share key order. Entity
    unique ids are built from the watch key, so it hashes the share key: a name
    or a position would move when a question is added.
    """
    groups: defaultdict[str, list[ConfigSubentry]] = defaultdict(list)
    for subentry in entry.subentries.values():
        if subentry.subentry_type == SUBENTRY_QUESTION:
            groups[share_key(subentry.data)].append(subentry)
    watches: list[WatchConfig] = []
    for number, shared in enumerate(sorted(groups), start=1):
        members = groups[shared]
        watches.append(
            watch_from_question(
                hass,
                members[0].data,
                key="ui_" + sha256(shared.encode()).hexdigest()[:12],
                name=members[0].title if len(members) == 1 else f"Group {number}",
                questions=[question_spec(member.data, _ui_key(member)) for member in members],
            )
        )
    return watches


def box_edges(answer: BoxAnswer) -> dict[str, float]:
    """The box's edges as fractions of the picture's width and height, measured
    from its top left corner, and the model's confidence in it."""
    return {side: getattr(answer, side) for side in BOX_SIDES}


def answer_as_dict(answer: Answer) -> dict[str, Any]:
    """One answer as plain data, for a template, an event or an action response.

    A choice and a score both carry their confidence and the probability of every
    option or level, so those two keys are added once for both.
    """
    match answer:
        case NoulAnswer():
            return {"type": TYPE_NOUL, "noul": answer.noul}
        case BoxAnswer():
            return {"type": TYPE_BOX, **box_edges(answer)}
        case ChoiceAnswer():
            own: dict[str, Any] = {"type": TYPE_CHOICE, "choice": answer.choice}
        case _:
            own = {
                "type": TYPE_SCORE,
                "score": answer.score,
                # Rubrics of different lengths compare only once each is
                # divided by its own top level.
                "normalized": round(answer.normalized, 4),
                "nearest_level": answer.nearest_level,
                "legend": answer.legend,
            }
    return {**own, "confidence": answer.confidence, "probabilities": answer.probabilities}
