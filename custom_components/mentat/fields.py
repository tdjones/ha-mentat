"""The fields of an AI Task structure as questions, and the answers back as field values.

A decision model fills boolean, select and number fields and nothing else: a
boolean is a noul, a select a choice over its values, a number a score cut into
levels. Each answer goes back as the field's own type, so a script reads a bool,
one of its options or a number on the field's scale. Any other selector, and any
field whose scale or options a question cannot carry, is refused here, before a
request is paid for.

Home Assistant 2026.9 hands the structure over as a voluptuous Schema. Each key
is a vol.Required or vol.Optional marker carrying the field's description, and
each value is a selector instance built from the field's `selector:` block. That
is why the table below is keyed on selector classes and not on type names.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from math import floor
from typing import Any, Final, NamedTuple

import voluptuous as vol
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import selector
from jevclient import (
    MAX_CHOICE_OPTIONS,
    MAX_SCORE_LEVELS,
    MIN_CHOICE_OPTIONS,
    MIN_SCORE_LEVELS,
    Answer,
    Choice,
    ChoiceAnswer,
    Noul,
    NoulAnswer,
    Question,
    Score,
    ScoreAnswer,
)

from .const import DOMAIN

# The result carries each field's probability or confidence under this key,
# since GenDataTaskResult has no other place for it. A field by this name would
# be overwritten, so it is refused.
RESERVED_KEY: Final = "mentat"

# A noul is a probability and a half favours neither answer, so a boolean field
# is true from there up. A question that needs another line is a question
# sensor with its own cutoff.
BOOLEAN_CUTOFF: Final = 0.5


def _refusal(key: str, **placeholders: str) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN, translation_key=key, translation_placeholders=placeholders
    )


@dataclass(frozen=True, slots=True)
class Scale:
    """A number field's range: bottom, top, and the step between valid values.

    A step of 0 means any value in the range is valid.
    """

    low: float
    high: float
    step: float

    @classmethod
    def from_selector(cls, key: str, instance: selector.NumberSelector) -> Scale:
        """The range a number selector declares.

        A number selector leaves out min and max unless they are written, and a
        score needs both, so a missing end is the usual refusal. It names the
        end to add.
        """
        config = instance.config
        low, high = config.get("min"), config.get("max")
        if low is None or high is None:
            raise _refusal(
                "field_needs_scale", field=key, bound="max" if low is not None else "min"
            )
        if not high > low:
            # The selector stores floats, and :g prints 10 where str() prints 10.0.
            raise _refusal("field_scale_not_ordered", field=key, min=f"{low:g}", max=f"{high:g}")
        step = config.get("step")
        return cls(float(low), float(high), float(step) if step not in (None, "any") else 0.0)

    def levels(self, unit: str) -> list[str]:
        """Evenly spaced rungs, each labelled with the value it maps back to.

        One rung per step when a score can hold that many, otherwise as many as
        it can. The labels go through value(), so the rung named as an answer's
        nearest_level and the number the field receives agree.
        """
        wanted = round((self.high - self.low) / self.step) + 1 if self.step else MAX_SCORE_LEVELS
        rungs = min(MAX_SCORE_LEVELS, max(MIN_SCORE_LEVELS, wanted))
        top = rungs - 1
        return [f"{self.value(rung / top)}{unit}".strip() for rung in range(rungs)]

    def value(self, fraction: float) -> int | float:
        """The number `fraction` of the way up, as the field accepts it.

        With a step, only multiples of it from the bottom are valid, so the value
        snaps to the nearest one and a half step goes up. round() would send 2.5
        to 2 and 3.5 to 4. A whole number comes back as an int, so a template
        prints 4 and not 4.0.
        """
        raw = self.low + (self.high - self.low) * fraction
        if self.step:
            raw = self.low + self.step * floor((raw - self.low) / self.step + 0.5)
        held = max(self.low, min(raw, self.high))
        return int(held) if held.is_integer() else round(held, 4)


def _ask_noul(key: str, wording: str, instance: selector.BooleanSelector) -> Question:
    return Noul(wording)


def _ask_choice(key: str, wording: str, instance: selector.SelectSelector) -> Question:
    """A choice over the select's values, each asked about once.

    An option is a plain string or a value and label pair. The value is what the
    script reads back, so the value is what the model chooses among. A repeated
    value counts once towards the minimum.
    """
    distinct = dict.fromkeys(
        str(option["value"]) if isinstance(option, Mapping) else option
        for option in instance.config["options"]
    )
    if not MIN_CHOICE_OPTIONS <= len(distinct) <= MAX_CHOICE_OPTIONS:
        raise _refusal(
            "field_options_out_of_range",
            field=key,
            count=str(len(distinct)),
            min=str(MIN_CHOICE_OPTIONS),
            max=str(MAX_CHOICE_OPTIONS),
        )
    return Choice(wording, dict.fromkeys(distinct))


def _ask_score(key: str, wording: str, instance: selector.NumberSelector) -> Question:
    scale = Scale.from_selector(key, instance)
    levels = scale.levels(instance.config.get("unit_of_measurement") or "")
    # A step wider than the range snaps two rungs onto one number. A score whose
    # levels repeat is a paid question with one possible answer.
    if len(levels) != len(set(levels)):
        raise _refusal(
            "field_step_too_coarse",
            field=key,
            min=f"{scale.low:g}",
            max=f"{scale.high:g}",
            step=f"{scale.step:g}",
        )
    return Score(wording, levels)


def _read_noul(key: str, instance: selector.BooleanSelector, answer: NoulAnswer) -> bool:
    return answer.noul >= BOOLEAN_CUTOFF


def _read_choice(key: str, instance: selector.SelectSelector, answer: ChoiceAnswer) -> str:
    return answer.choice


def _read_score(key: str, instance: selector.NumberSelector, answer: ScoreAnswer) -> int | float:
    return Scale.from_selector(key, instance).value(answer.normalized)


class _Kind(NamedTuple):
    """How one selector class becomes a question, and its answer a value."""

    to_question: Callable[[str, str, Any], Question]
    answer: type[Answer]
    to_value: Callable[[str, Any, Any], Any]


_KINDS: Final[dict[type, _Kind]] = {
    selector.BooleanSelector: _Kind(_ask_noul, NoulAnswer, _read_noul),
    selector.SelectSelector: _Kind(_ask_choice, ChoiceAnswer, _read_choice),
    selector.NumberSelector: _Kind(_ask_score, ScoreAnswer, _read_score),
}


def _kind(instance: Any) -> _Kind | None:
    """The table row for this selector, a subclass finding its parent's."""
    return next((_KINDS[cls] for cls in type(instance).__mro__ if cls in _KINDS), None)


def _fields(structure: vol.Schema) -> Iterator[tuple[str, str, Any]]:
    """Each field's key, what to ask about it, and its selector, in declared order.

    A field with no description is asked about by its key. That is worth
    something for `gate_left_open` and nothing for `value`, which is why a field
    is worth a description.
    """
    for marker, instance in structure.schema.items():
        key = str(marker.schema) if isinstance(marker, vol.Marker) else str(marker)
        description = getattr(marker, "description", None)
        yield key, str(description) if description else key, instance


def questions_from_structure(structure: vol.Schema) -> dict[str, Question]:
    """One question per field, keyed and ordered as the task declared them."""
    questions: dict[str, Question] = {}
    for key, wording, instance in _fields(structure):
        if key == RESERVED_KEY:
            raise _refusal("reserved_field", field=RESERVED_KEY)
        kind = _kind(instance)
        if kind is None:
            raise _refusal("unsupported_field", field=key, selector=type(instance).__name__)
        questions[key] = kind.to_question(key, wording, instance)
    if not questions:
        raise _refusal("no_fields")
    return questions


def values_from_answers(structure: vol.Schema, answers: Mapping[str, Answer]) -> dict[str, Any]:
    """Each answer as the value its field asked for.

    A missing answer is an error and not None. A template reading a value that
    is not there renders an empty string, which reads like an answer.
    """
    values: dict[str, Any] = {}
    for key, _wording, instance in _fields(structure):
        if key not in answers:
            raise _refusal("answer_missing", field=key)
        answer = answers[key]
        kind = _kind(instance)
        if kind is None or not isinstance(answer, kind.answer):
            raise _refusal(
                "answer_does_not_fit",
                field=key,
                answer=type(answer).__name__,
                selector=type(instance).__name__,
            )
        values[key] = kind.to_value(key, instance, answer)
    return values
