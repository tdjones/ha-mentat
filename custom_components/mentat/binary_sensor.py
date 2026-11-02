"""Binary sensors: a noul's verdict against its cutoff, and the budget tripwire."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from jevclient import NoulAnswer

from .const import (
    ATTR_PROBABILITY,
    CONF_CUTOFF,
    CONF_TOKEN_BUDGET,
    UID_OVER_BUDGET,
    UID_TOKENS,
    UID_VERDICT,
    WINDOW_ATTRIBUTES,
)
from .coordinator import MentatCoordinator, verdict
from .entity import (
    MentatLedgerEntity,
    MentatQuestionEntity,
    async_add_current_entities,
    budget_or_none,
)

if TYPE_CHECKING:
    from . import MentatConfigEntry

# These read answers a coordinator already fetched.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MentatConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    verdicts = [
        MentatVerdictSensor(coordinator, spec.key)
        for coordinator in runtime.coordinators.values()
        for spec in coordinator.watch_config.questions
        if spec.wants_verdict
    ]
    tripwire = MentatOverBudgetSensor(entry, runtime.ledger)
    entities: list[Entity] = [tripwire, *verdicts]
    async_add_current_entities(hass, entry, Platform.BINARY_SENSOR, entities, async_add_entities)


class MentatVerdictSensor(MentatQuestionEntity, BinarySensorEntity):
    """A noul compared with its cutoff, so an automation can trigger on a state.

    Only questions that want a verdict get one: a noul with a cutoff.
    """

    _unrecorded_attributes = WINDOW_ATTRIBUTES
    _attr_translation_key = UID_VERDICT

    def __init__(self, coordinator: MentatCoordinator, key: str) -> None:
        super().__init__(coordinator, key, UID_VERDICT)

    @property
    def is_on(self) -> bool | None:
        return verdict(self.spec, self.answer)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        answer = self.answer
        if not isinstance(answer, NoulAnswer):
            return None
        attributes = {ATTR_PROBABILITY: round(answer.noul, 3), CONF_CUTOFF: self.spec.cutoff}
        return attributes | self._window_attributes()


class MentatOverBudgetSensor(MentatLedgerEntity, BinarySensorEntity):
    """On while the daily token budget holds requests back."""

    _unrecorded_attributes = frozenset()
    _suffix = UID_OVER_BUDGET
    _ledger_attributes = (
        (CONF_TOKEN_BUDGET, budget_or_none),
        (UID_TOKENS, lambda ledger: ledger.tokens),
    )
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    @property
    def is_on(self) -> bool:
        return self._ledger.over_budget
