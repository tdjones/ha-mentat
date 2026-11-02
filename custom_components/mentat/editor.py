"""The question editor: add or change one question in the UI.

Each question is one config subentry. The flow opens on a menu of answer kinds
and then shows that kind's form. The fields run in the order a question gets
thought out: its name, what it asks, the shape of its answer, what it reads.
The schedule and the window sit in collapsed sections, because their defaults
suit a first question. A field a selector cannot check is checked here, and
the error goes on that field. A valid form goes on to the preview step, which
shows what preview.py builds, and only submitting that saves the question.

The editor never groups questions into watches. watches_from_subentries does
that from the saved data, so the form keys are the keys it and
question_from_mapping read, and the saved data is the form with its sections
lifted to the top.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final

import voluptuous as vol
from homeassistant.config_entries import ConfigSubentryFlow, SubentryFlowResult
from homeassistant.const import CONF_NAME, CONF_SCAN_INTERVAL, CONF_TYPE
from homeassistant.data_entry_flow import SectionConfig, section
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import selector as sel
from jevclient import MAX_CHOICE_OPTIONS, MAX_SCORE_LEVELS, MIN_CHOICE_OPTIONS, MIN_SCORE_LEVELS

from .const import (
    CONF_ADVANCED,
    CONF_BRIEF,
    CONF_CUTOFF,
    CONF_HIGH,
    CONF_INSTRUCTIONS,
    CONF_LOW,
    CONF_NOTE,
    CONF_OPTIONS,
    CONF_SCALE,
    CONF_TARGET,
    CONF_WAKE_ON,
    CONF_WINDOW_COLLAPSE_REPEATS,
    CONF_WINDOW_DURATION,
    CONF_WINDOW_ENABLED,
    CONF_WINDOW_EVALUATE,
    CONF_WINDOW_EVENTS,
    CONF_WINDOW_MAX_EVENTS,
    CONF_WINDOW_SETTLE,
    CONF_WITH_ATTRIBUTES,
    DEFAULT_EDITOR_WINDOW_MINUTES,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DEFAULT_WINDOW_EVENTS,
    DEFAULT_WINDOW_SETTLE_SECONDS,
    EVALUATE_BOTH,
    EVALUATE_MODES,
    MAX_SCAN_INTERVAL_SECONDS,
    MAX_WINDOW_EVENTS,
    MAX_WINDOW_SECONDS,
    MAX_WINDOW_SETTLE_SECONDS,
    MIN_SCAN_INTERVAL_SECONDS,
    MIN_WINDOW_EVENTS,
    MIN_WINDOW_SECONDS,
    SECTION_WINDOW,
    TYPE_CHOICE,
    TYPE_NOUL,
    TYPE_SCORE,
)
from .preview import describe_sharing, describe_window, draft_state, trial
from .schema import EVENT_FILTER_SCHEMA
from .watches import is_own_event, options_from, scale_from

# Errors that belong to no one field, or to a field inside a collapsed
# section, where nobody would see them.
BASE: Final = "base"
ALL_KINDS: Final = frozenset({TYPE_NOUL, TYPE_CHOICE, TYPE_SCORE})

type _Input = dict[str, Any] | None


def _option_names(text: str) -> Counter[str]:
    return Counter(line.partition(":")[0].strip() for line in text.splitlines() if line.strip())


def option_problem(text: str) -> str | None:
    """The error key for an options box that would lose an option, or None.

    A line with nothing before its colon and a name written twice both parse
    without complaint, and each drops an option, so the question sent would
    not be the one on screen.
    """
    names = _option_names(text)
    if "" in names:
        return "option_unnamed"
    if any(count > 1 for count in names.values()):
        return "option_repeated"
    return None


def _box(
    low: float, high: float, *, step: float | None = None, unit: str | None = None
) -> sel.NumberSelector:
    config = sel.NumberSelectorConfig(min=low, max=high, mode=sel.NumberSelectorMode.BOX)
    if step is not None:
        config["step"] = step
    if unit is not None:
        config["unit_of_measurement"] = unit
    return sel.NumberSelector(config)


_LINES: Final = sel.TextSelector(sel.TextSelectorConfig(multiline=True))


@dataclass(frozen=True, slots=True)
class _Field:
    """One form field: its key, its widget, the section it sits in and which forms have it."""

    key: str
    widget: sel.Selector[Any]
    section: str | None = None
    required: bool = False
    default: Any = vol.UNDEFINED
    kinds: frozenset[str] = ALL_KINDS

    def marker(self) -> vol.Marker:
        if self.required:
            return vol.Required(self.key)
        return vol.Optional(self.key, default=self.default)


_FIELDS: Final[tuple[_Field, ...]] = (
    _Field(CONF_NAME, sel.TextSelector(), required=True),
    _Field(CONF_INSTRUCTIONS, _LINES, required=True),
    _Field(CONF_HIGH, sel.TextSelector(), kinds=frozenset({TYPE_NOUL})),
    _Field(CONF_LOW, sel.TextSelector(), kinds=frozenset({TYPE_NOUL})),
    _Field(CONF_CUTOFF, _box(0, 1, step=0.01), kinds=frozenset({TYPE_NOUL})),
    _Field(CONF_OPTIONS, _LINES, required=True, kinds=frozenset({TYPE_CHOICE})),
    _Field(CONF_SCALE, _LINES, required=True, kinds=frozenset({TYPE_SCORE})),
    _Field(CONF_TARGET, sel.TargetSelector()),
    _Field(CONF_NOTE, sel.TemplateSelector()),
    _Field(CONF_BRIEF, _LINES),
    _Field(
        CONF_WAKE_ON,
        sel.EntitySelector(sel.EntitySelectorConfig(multiple=True)),
        section=CONF_ADVANCED,
    ),
    _Field(
        CONF_SCAN_INTERVAL,
        _box(MIN_SCAN_INTERVAL_SECONDS, MAX_SCAN_INTERVAL_SECONDS, unit="s"),
        section=CONF_ADVANCED,
        default=DEFAULT_SCAN_INTERVAL_SECONDS,
    ),
    _Field(CONF_WITH_ATTRIBUTES, sel.BooleanSelector(), section=CONF_ADVANCED, default=False),
    _Field(CONF_WINDOW_ENABLED, sel.BooleanSelector(), section=SECTION_WINDOW, default=False),
    _Field(
        CONF_WINDOW_DURATION,
        sel.DurationSelector(),
        section=SECTION_WINDOW,
        default={"hours": 0, "minutes": DEFAULT_EDITOR_WINDOW_MINUTES, "seconds": 0},
    ),
    _Field(
        CONF_WINDOW_MAX_EVENTS,
        _box(MIN_WINDOW_EVENTS, MAX_WINDOW_EVENTS),
        section=SECTION_WINDOW,
        default=DEFAULT_WINDOW_EVENTS,
    ),
    _Field(
        CONF_WINDOW_SETTLE,
        _box(0, MAX_WINDOW_SETTLE_SECONDS, unit="s"),
        section=SECTION_WINDOW,
        default=DEFAULT_WINDOW_SETTLE_SECONDS,
    ),
    _Field(
        CONF_WINDOW_EVALUATE,
        sel.SelectSelector(
            sel.SelectSelectorConfig(
                options=list(EVALUATE_MODES),
                translation_key=CONF_WINDOW_EVALUATE,
                mode=sel.SelectSelectorMode.DROPDOWN,
            )
        ),
        section=SECTION_WINDOW,
        default=EVALUATE_BOTH,
    ),
    _Field(CONF_WINDOW_EVENTS, sel.ObjectSelector(), section=SECTION_WINDOW),
    _Field(
        CONF_WINDOW_COLLAPSE_REPEATS, sel.BooleanSelector(), section=SECTION_WINDOW, default=True
    ),
)

# Every field kept in a section, mapped to that section.
_HOME: Final[dict[str, str]] = {field.key: field.section for field in _FIELDS if field.section}
_SECTIONS: Final[tuple[str, ...]] = (CONF_ADVANCED, SECTION_WINDOW)


def _form(kind: str) -> vol.Schema:
    top: dict[vol.Marker, Any] = {}
    inside: dict[str, dict[vol.Marker, Any]] = {name: {} for name in _SECTIONS}
    for field in _FIELDS:
        if kind in field.kinds:
            (top if field.section is None else inside[field.section])[field.marker()] = field.widget
    for name, fields in inside.items():
        top[vol.Required(name)] = section(vol.Schema(fields), SectionConfig(collapsed=True))
    return vol.Schema(top)


FORMS: Final[dict[str, vol.Schema]] = {
    kind: _form(kind) for kind in (TYPE_NOUL, TYPE_CHOICE, TYPE_SCORE)
}


def flatten(user_input: Mapping[str, Any]) -> dict[str, Any]:
    """The form's answers with the sections lifted out, the way they are saved."""
    flat: dict[str, Any] = {}
    for key, value in user_input.items():
        if key in _SECTIONS:
            flat.update(value or {})
        else:
            flat[key] = value
    return flat


def nest(data: Mapping[str, Any]) -> dict[str, Any]:
    """Saved data put back into its sections, to fill the form in again."""
    nested: dict[str, Any] = {name: {} for name in _SECTIONS}
    for key, value in data.items():
        if key in _HOME:
            nested[_HOME[key]][key] = value
        else:
            nested[key] = value
    return nested


# --- validation ---


def _has_target(data: Mapping[str, Any]) -> bool:
    # The picker sends {"entity_id": []} once something was picked and removed.
    return any((data.get(CONF_TARGET) or {}).values())


def _windowed(data: Mapping[str, Any]) -> bool:
    return bool(data.get(CONF_WINDOW_ENABLED))


def _window_length(data: Mapping[str, Any]) -> float:
    length: timedelta = cv.time_period_dict(data[CONF_WINDOW_DURATION])
    return length.total_seconds()


def _bus_events(data: Mapping[str, Any]) -> Any:
    return data.get(CONF_WINDOW_EVENTS) or []


def _logs_own_event(data: Mapping[str, Any]) -> bool:
    events = _bus_events(data)
    return isinstance(events, list) and any(
        isinstance(event, Mapping) and is_own_event(event.get("event_type", "")) for event in events
    )


def _events_malformed(data: Mapping[str, Any]) -> bool:
    try:
        vol.Schema([EVENT_FILTER_SCHEMA])(_bus_events(data))
    except vol.Invalid:
        return True
    return False


def _options_text(data: Mapping[str, Any]) -> str:
    return str(data.get(CONF_OPTIONS, ""))


def _is_choice(data: Mapping[str, Any]) -> bool:
    return bool(data[CONF_TYPE] == TYPE_CHOICE)


type _Rule = tuple[Callable[[Mapping[str, Any]], bool], str, str]

# Read top to bottom; a field keeps the first error found for it. The window
# rules come first so a window with no target says that, rather than the more
# general nothing_to_read. They are the YAML schema's window rules in form shape.
_RULES: Final[tuple[_Rule, ...]] = (
    (lambda d: _windowed(d) and not _has_target(d), BASE, "window_without_target"),
    (
        lambda d: (
            _windowed(d) and not MIN_WINDOW_SECONDS <= _window_length(d) <= MAX_WINDOW_SECONDS
        ),
        BASE,
        "window_duration_out_of_range",
    ),
    (
        lambda d: (
            _windowed(d)
            and float(d.get(CONF_WINDOW_SETTLE, DEFAULT_WINDOW_SETTLE_SECONDS)) > _window_length(d)
        ),
        BASE,
        "window_settle_too_long",
    ),
    # Ahead of the schema check, which refuses these too, so the error says why.
    (lambda d: _windowed(d) and _logs_own_event(d), BASE, "window_event_is_own"),
    (lambda d: _windowed(d) and _events_malformed(d), BASE, "window_events_invalid"),
    (lambda d: not _has_target(d) and not d.get(CONF_NOTE), BASE, "nothing_to_read"),
    (
        lambda d: _is_choice(d) and option_problem(_options_text(d)) == "option_unnamed",
        CONF_OPTIONS,
        "option_unnamed",
    ),
    (
        lambda d: _is_choice(d) and option_problem(_options_text(d)) == "option_repeated",
        CONF_OPTIONS,
        "option_repeated",
    ),
    (
        lambda d: (
            _is_choice(d)
            and not MIN_CHOICE_OPTIONS <= len(options_from(_options_text(d))) <= MAX_CHOICE_OPTIONS
        ),
        CONF_OPTIONS,
        "options_count",
    ),
    (
        lambda d: (
            d[CONF_TYPE] == TYPE_SCORE
            and not MIN_SCORE_LEVELS <= len(scale_from(d.get(CONF_SCALE, ""))) <= MAX_SCORE_LEVELS
        ),
        CONF_SCALE,
        "scale_count",
    ),
)


def validate(kind: str, data: Mapping[str, Any]) -> dict[str, str]:
    """Each field's first problem, keyed by the field, or "base" for the form."""
    typed = {**data, CONF_TYPE: kind}
    errors: dict[str, str] = {}
    for broken, field, key in _RULES:
        if field not in errors and broken(typed):
            errors[field] = key
    return errors


# --- the flow ---


class MentatQuestionSubentryFlow(ConfigSubentryFlow):
    """One question, added from the menu or changed in place."""

    def __init__(self) -> None:
        self._draft: dict[str, Any] = {}
        self._editing: str | None = None

    async def async_step_user(self, user_input: _Input = None) -> SubentryFlowResult:
        """Ask which kind of answer the question wants."""
        return self.async_show_menu(step_id="user", menu_options=list(FORMS))

    async def async_step_noul(self, user_input: _Input = None) -> SubentryFlowResult:
        return await self._take("noul", TYPE_NOUL, user_input)

    async def async_step_choice(self, user_input: _Input = None) -> SubentryFlowResult:
        return await self._take("choice", TYPE_CHOICE, user_input)

    async def async_step_score(self, user_input: _Input = None) -> SubentryFlowResult:
        return await self._take("score", TYPE_SCORE, user_input)

    async def async_step_reconfigure(self, user_input: _Input = None) -> SubentryFlowResult:
        """Change a saved question, on the form for the kind it already is."""
        saved = self._get_reconfigure_subentry()
        self._editing = saved.subentry_id
        kind: str = saved.data[CONF_TYPE]
        return await self._take("reconfigure", kind, user_input, filled=saved.data)

    async def _take(
        self,
        step_id: str,
        kind: str,
        user_input: _Input,
        *,
        filled: Mapping[str, Any] | None = None,
    ) -> SubentryFlowResult:
        """Show a kind's form, or check what came back and move on to the preview."""
        if user_input is None:
            return self._show(step_id, kind, filled, {})
        data = flatten(user_input)
        if errors := validate(kind, data):
            return self._show(step_id, kind, data, errors)
        self._draft = data | {CONF_TYPE: kind}
        return await self.async_step_preview()

    def _show(
        self,
        step_id: str,
        kind: str,
        filled: Mapping[str, Any] | None,
        errors: dict[str, str],
    ) -> SubentryFlowResult:
        form = FORMS[kind]
        if filled is not None:
            form = self.add_suggested_values_to_schema(form, nest(filled))
        return self.async_show_form(step_id=step_id, data_schema=form, errors=errors)

    async def async_step_preview(self, user_input: _Input = None) -> SubentryFlowResult:
        """Show what the draft reads and answers now. Submitting saves it."""
        if user_input is not None:
            return self._save()
        entry = self._get_entry()
        shown, state = await draft_state(self.hass, self._draft)
        notes = [await describe_sharing(self.hass, entry, self._draft, self._editing)]
        if window := await describe_window(self.hass, state):
            notes.append(window)
        return self.async_show_form(
            step_id="preview",
            data_schema=vol.Schema({}),
            description_placeholders={
                "state": shown,
                "grouping": "\n\n".join(notes),
                "answer": await trial(self.hass, entry, self._draft, state),
            },
        )

    def _save(self) -> SubentryFlowResult:
        title: str = self._draft[CONF_NAME]
        if self._editing is None:
            return self.async_create_entry(title=title, data=self._draft)
        return self.async_update_and_abort(
            self._get_entry(), self._get_reconfigure_subentry(), title=title, data=self._draft
        )
