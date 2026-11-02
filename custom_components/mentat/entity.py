"""Entity bases, and the one service device every entity of an entry joins.

Three bases. A watch entity reads one coordinator and has the unique id it is
given. A question entity is a watch entity for one question, looked up by key,
and unavailable until that question has an answer.
A ledger entity reads the entry's day of tokens and is written whenever the
ledger changes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_MODEL, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from jevclient import Answer

from .budget import Ledger
from .const import DOMAIN, WINDOW_ATTRIBUTES
from .coordinator import MentatCoordinator, unique_id
from .providers import provider_for
from .watches import QuestionSpec


def build_device_info(entry: ConfigEntry) -> DeviceInfo:
    """The entry's service device, named after the entry and made by its provider.

    The AI Task entity and every sensor of the entry hang off this one device.
    """
    preset = provider_for(entry.data)
    return DeviceInfo(
        entry_type=DeviceEntryType.SERVICE,
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer=preset.title,
        model=entry.data.get(CONF_MODEL) or preset.model,
        name=entry.title,
    )


def budget_or_none(ledger: Ledger) -> int | None:
    """The day's token budget for an attribute. 0 means no limit, shown as None."""
    return ledger.budget or None


@callback
def async_add_current_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    platform: str,
    entities: list[Entity],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a platform's entities, and drop the ones the entry no longer provides.

    A deleted question, a cleared cutoff or a YAML question taken out would
    otherwise stay in the registry, unavailable, until removed by hand.
    """
    wanted = {entity.unique_id for entity in entities}
    registry = er.async_get(hass)
    stale = [
        registered.entity_id
        for registered in er.async_entries_for_config_entry(registry, entry.entry_id)
        if registered.domain == platform and registered.unique_id not in wanted
    ]
    for entity_id in stale:
        registry.async_remove(entity_id)
    async_add_entities(entities)


class MentatWatchEntity(CoordinatorEntity[MentatCoordinator]):
    """An entity fed by one watch's coordinator, on the entry's service device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: MentatCoordinator, unique: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = unique

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(self.coordinator.config_entry)


class MentatQuestionEntity(MentatWatchEntity):
    """An entity showing one question's answer, named after the question."""

    # These change on every evaluation. Recording them would store a new set of
    # attributes each time for a history nobody reads.
    _unrecorded_attributes = WINDOW_ATTRIBUTES

    def __init__(self, coordinator: MentatCoordinator, key: str, *suffix: str) -> None:
        super().__init__(coordinator, unique_id(coordinator.id_scope, key, *suffix))
        self._key = key

    @property
    def spec(self) -> QuestionSpec:
        """The question as the coordinator's watch has it."""
        return next(
            spec for spec in self.coordinator.watch_config.questions if spec.key == self._key
        )

    @property
    def name(self) -> str:
        """The name the user gave the question."""
        return self.spec.name

    @property
    def answer(self) -> Answer | None:
        """This question's answer from the last evaluation that worked."""
        return (self.coordinator.data or {}).get(self._key)

    @property
    def available(self) -> bool:
        """False after a failed evaluation, and while the reply has no answer under this key.

        Nothing stands in for a missing answer.
        """
        return self.answer is not None and super().available

    def _window_attributes(self) -> dict[str, Any]:
        """Which window the answer is about. Empty for a watch without one."""
        span = self.coordinator.window_span
        return {} if span is None else span.as_attributes()


class MentatLedgerEntity(Entity):
    """A diagnostic entity reading the entry's ledger.

    `_suffix` is both the unique id suffix and the translation key.
    `_ledger_attributes` pairs each attribute name with how it is read.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _suffix: ClassVar[str]
    _ledger_attributes: ClassVar[tuple[tuple[str, Callable[[Ledger], Any]], ...]] = ()

    def __init__(self, entry: ConfigEntry, ledger: Ledger) -> None:
        self._ledger = ledger
        self._attr_unique_id = unique_id(entry.entry_id, self._suffix)
        self._attr_translation_key = self._suffix
        self._attr_device_info = build_device_info(entry)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        ledger = self._ledger
        return {name: read(ledger) for name, read in self._ledger_attributes} or None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._ledger.async_add_listener(self.async_write_ha_state))
