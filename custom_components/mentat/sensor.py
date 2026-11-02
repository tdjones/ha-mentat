"""Sensors: one per question, two diagnostics per watch, and the day's ledger.

A noul's sensor reads as the probability of yes, a choice's as the winning
option, and a score's as the probability-weighted level, which can land between
two levels. Each answer type has one row in `_KINDS` saying how it reads.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, Platform, UnitOfInformation, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util
from jevclient import Answer, ChoiceAnswer, NoulAnswer, ScoreAnswer

from .const import (
    ATTR_CONFIDENCE,
    ATTR_LEGEND,
    ATTR_NEAREST_LEVEL,
    ATTR_PROBABILITIES,
    ATTR_QUESTION_TYPE,
    ATTR_QUESTIONS,
    ATTR_SENT_STATE,
    CONF_TOKEN_BUDGET,
    CONF_TOKEN_PRICE,
    TYPE_CHOICE,
    TYPE_NOUL,
    TYPE_SCORE,
    UID_BYTES,
    UID_LATENCY,
    UID_REQUESTS,
    UID_SPEND,
    UID_TOKENS,
    WINDOW_ATTRIBUTES,
)
from .coordinator import MentatCoordinator
from .entity import (
    MentatLedgerEntity,
    MentatQuestionEntity,
    MentatWatchEntity,
    async_add_current_entities,
    budget_or_none,
)

if TYPE_CHECKING:
    from . import MentatConfigEntry

# Every sensor reads what a coordinator or the ledger already holds.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MentatConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    ledger_sensors = (MentatRequestsSensor, MentatTokensSensor, MentatSpendSensor)
    sensors: list[Entity] = [kind(entry, runtime.ledger) for kind in ledger_sensors]
    for coordinator in runtime.coordinators.values():
        sensors.extend(_watch_sensors(coordinator))
    async_add_current_entities(hass, entry, Platform.SENSOR, sensors, async_add_entities)


def _watch_sensors(coordinator: MentatCoordinator) -> Iterator[Entity]:
    """A sensor per question, then the watch's two diagnostics."""
    for spec in coordinator.watch_config.questions:
        yield MentatQuestionSensor(coordinator, spec.key)
    yield MentatWatchLatencySensor(coordinator)
    yield MentatWatchBytesSensor(coordinator)


def _rounded(distribution: dict[str, float]) -> dict[str, float]:
    return {key: round(value, 4) for key, value in distribution.items()}


def _spread(answer: ChoiceAnswer | ScoreAnswer) -> dict[str, Any]:
    return {
        ATTR_CONFIDENCE: round(answer.confidence, 3),
        ATTR_PROBABILITIES: _rounded(answer.probabilities),
    }


@dataclass(frozen=True, slots=True)
class _Kind:
    """How one answer type reads as a sensor."""

    # The answer the question expects. Anything else is a broken promise from
    # the server, and showing it would put a word in a numeric sensor.
    answer: type[Answer]
    value: Callable[[Any], float | str]
    attributes: Callable[[Any], dict[str, Any]]
    # A choice is an enum of its options; the other two are measurements.
    is_enum: bool = False


_KINDS: dict[str, _Kind] = {
    # A noul carries no confidence: the probability is the whole answer.
    TYPE_NOUL: _Kind(NoulAnswer, lambda a: round(a.noul, 3), lambda a: {}),
    TYPE_CHOICE: _Kind(ChoiceAnswer, lambda a: a.choice, _spread, is_enum=True),
    TYPE_SCORE: _Kind(
        ScoreAnswer,
        lambda a: round(a.score, 3),
        lambda a: {**_spread(a), ATTR_LEGEND: a.legend, ATTR_NEAREST_LEVEL: a.nearest_level},
    ),
}


class MentatQuestionSensor(MentatQuestionEntity, SensorEntity):
    """The answer to one question."""

    _unrecorded_attributes = WINDOW_ATTRIBUTES

    def __init__(self, coordinator: MentatCoordinator, key: str) -> None:
        super().__init__(coordinator, key)
        spec = self.spec
        self._type = spec.kind
        self._kind = _KINDS[spec.kind]
        # For the icon only: the name is the user's own.
        self._attr_translation_key = spec.kind
        if self._kind.is_enum:
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = list(spec.choices)
        else:
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_suggested_display_precision = 2

    def _typed_answer(self) -> Answer | None:
        """The answer, or None when it is missing, of another type, or no option of the enum.

        Home Assistant refuses to write an enum sensor's state outside its
        options, and would refuse it again on every evaluation.
        """
        answer = self.answer
        if not isinstance(answer, self._kind.answer):
            return None
        if isinstance(answer, ChoiceAnswer) and answer.choice not in self.spec.choices:
            return None
        return answer

    @property
    def native_value(self) -> float | str | None:
        answer = self._typed_answer()
        return None if answer is None else self._kind.value(answer)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if (answer := self._typed_answer()) is None:
            return None
        return {
            ATTR_QUESTION_TYPE: self._type,
            **self._kind.attributes(answer),
            **self._window_attributes(),
        }


class _MentatWatchDiagnostic(MentatWatchEntity, SensorEntity):
    """A diagnostic about one watch's last request.

    The watch's name is the user's word, so it goes in as a placeholder.
    """

    _suffix: ClassVar[str]

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    # Off by default: the bytes sensor carries the whole request, which every
    # open dashboard would be sent on every evaluation.
    _attr_entity_registry_enabled_default = False
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: MentatCoordinator) -> None:
        watch = coordinator.watch_config
        super().__init__(coordinator, coordinator.diagnostic_id(self._suffix))
        self._attr_translation_key = f"watch_{self._suffix}"
        self._attr_translation_placeholders = {"watch": watch.name}


class MentatWatchLatencySensor(_MentatWatchDiagnostic):
    """How long the last answered request took."""

    _unrecorded_attributes = frozenset()
    _suffix = UID_LATENCY
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MILLISECONDS

    @property
    def native_value(self) -> int | None:
        last = self.coordinator.last
        return None if last is None else round(last.latency_ms)


class MentatWatchBytesSensor(_MentatWatchDiagnostic):
    """What the watch last sent, and how many bytes it was."""

    # The recorder would otherwise keep a copy of the house's state for every
    # evaluation.
    _unrecorded_attributes = frozenset({ATTR_SENT_STATE, ATTR_QUESTIONS})
    _suffix = UID_BYTES
    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_native_unit_of_measurement = UnitOfInformation.BYTES

    @property
    def native_value(self) -> int | None:
        last = self.coordinator.last
        return None if last is None else last.body_bytes

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The last request that was answered. A failure leaves it standing."""
        last = self.coordinator.last
        questions = self.coordinator.watch_config.questions
        return {
            ATTR_SENT_STATE: None if last is None else last.state,
            ATTR_QUESTIONS: {spec.key: spec.question.as_payload() for spec in questions},
        }


class MentatRequestsSensor(MentatLedgerEntity, SensorEntity):
    """Requests answered today, from midnight in the Home Assistant time zone."""

    _unrecorded_attributes = frozenset()
    _suffix = UID_REQUESTS
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    @property
    def native_value(self) -> int:
        return self._ledger.requests


class MentatTokensSensor(MentatLedgerEntity, SensorEntity):
    """Input tokens today, as the server reported them."""

    _unrecorded_attributes = frozenset()
    _suffix = UID_TOKENS
    _ledger_attributes = ((CONF_TOKEN_BUDGET, budget_or_none),)
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "tokens"

    @property
    def native_value(self) -> int:
        return self._ledger.tokens


class MentatSpendSensor(MentatLedgerEntity, SensorEntity):
    """Estimated spend today: measured tokens times the configured price."""

    _unrecorded_attributes = frozenset()
    _suffix = UID_SPEND
    _ledger_attributes = ((CONF_TOKEN_PRICE, lambda ledger: ledger.token_price),)
    _attr_device_class = SensorDeviceClass.MONETARY
    # Monetary allows total only; last_reset tells statistics where a day starts.
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "USD"
    _attr_suggested_display_precision = 4

    @property
    def native_value(self) -> float:
        return round(self._ledger.spend, 6)

    @property
    def last_reset(self) -> datetime:
        return dt_util.start_of_local_day(self._ledger.day)
