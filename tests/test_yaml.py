"""YAML watches: which entry runs them, how they move on, and what is refused."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import DEFAULT, MagicMock

import pytest
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from jevclient import JevConnectionError, NoulAnswer
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mentat import YAML_WATCHES
from custom_components.mentat.const import DOMAIN

from .conftest import (
    ENTRY_DATA,
    NOUL_KEY,
    PROBE_TOKENS,
    WASHER,
    WATCH,
    LoadYaml,
    build_response,
    laundry_answers,
)

FORGOTTEN_ID = f"yaml:{NOUL_KEY}"


async def test_async_setup_two_yaml_watches_get_a_coordinator_each(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    fake_client.ask.return_value = build_response(**laundry_answers())
    second = {**WATCH, "name": "Dryer", "questions": WATCH["questions"][:1]}
    entry = await yaml_watch([WATCH, second])
    assert sorted(entry.runtime_data.coordinators) == ["dryer", "laundry"]
    assert hass.states.get("sensor.typesafe_laundry_forgotten").state == "0.81"
    # The dryer's key differs, so the reply carries no answer for it.
    assert hass.states.get("sensor.typesafe_laundry_forgotten_2").state == "unavailable"


async def test_async_setup_refuses_two_watches_named_alike(hass: HomeAssistant) -> None:
    assert not await async_setup_component(
        hass, DOMAIN, {DOMAIN: [WATCH, {**WATCH, "name": "LAUNDRY"}]}
    )


async def test_async_setup_refuses_a_watch_with_no_questions(hass: HomeAssistant) -> None:
    assert not await async_setup_component(hass, DOMAIN, {DOMAIN: [{**WATCH, "questions": []}]})


async def test_async_setup_gives_yaml_to_the_first_entry_only(
    hass: HomeAssistant, fake_client: MagicMock
) -> None:
    first = MockConfigEntry(domain=DOMAIN, title="first", data=ENTRY_DATA["typesafe"])
    second = MockConfigEntry(domain=DOMAIN, title="second", data=ENTRY_DATA["generic"])
    first.add_to_hass(hass)
    second.add_to_hass(hass)
    hass.states.async_set("sensor.washer_power", "1.2")
    # Setting up the integration sets up both entries.
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: [WATCH]})
    await hass.async_block_till_done()
    assert list(first.runtime_data.coordinators) == ["laundry"]
    assert second.runtime_data.coordinators == {}
    evaluations = [c for c in fake_client.ask.await_args_list if "probe" not in c.args[1]]
    assert len(evaluations) == 1


async def test_async_setup_without_yaml_has_no_coordinators(
    hass: HomeAssistant, fake_client: MagicMock, loaded: MockConfigEntry
) -> None:
    assert loaded.runtime_data.coordinators == {}


# --- when the entry running them goes ---


@pytest.fixture
def pair(hass: HomeAssistant, fake_client: MagicMock) -> tuple[MockConfigEntry, MockConfigEntry]:
    """Two entries, added in order, answering every laundry question."""
    fake_client.ask.return_value = build_response(**laundry_answers())
    first = MockConfigEntry(domain=DOMAIN, title="first", data=ENTRY_DATA["typesafe"])
    second = MockConfigEntry(domain=DOMAIN, title="second", data=ENTRY_DATA["generic"])
    first.add_to_hass(hass)
    second.add_to_hass(hass)
    hass.states.async_set(WASHER, "1.2")
    return first, second


async def start(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: [WATCH]})
    await hass.async_block_till_done()


def forgotten(hass: HomeAssistant) -> er.RegistryEntry:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, FORGOTTEN_ID)
    assert entity_id is not None
    found = registry.async_get(entity_id)
    assert found is not None
    return found


async def test_yaml_moves_on_when_its_entry_is_disabled(
    hass: HomeAssistant, pair: tuple[MockConfigEntry, MockConfigEntry]
) -> None:
    first, second = pair
    await start(hass)
    before = forgotten(hass)
    assert before.config_entry_id == first.entry_id

    assert await hass.config_entries.async_set_disabled_by(first.entry_id, ConfigEntryDisabler.USER)
    await hass.async_block_till_done()
    assert list(second.runtime_data.coordinators) == ["laundry"]
    after = forgotten(hass)
    # The same entity, now on the second entry's device and still enabled.
    assert after.entity_id == before.entity_id
    assert after.config_entry_id == second.entry_id
    assert after.disabled_by is None
    assert hass.states.get(after.entity_id).state == "0.81"


async def test_yaml_moves_on_when_its_entry_is_removed(
    hass: HomeAssistant, pair: tuple[MockConfigEntry, MockConfigEntry]
) -> None:
    first, second = pair
    await start(hass)
    entity_id = forgotten(hass).entity_id
    await hass.config_entries.async_remove(first.entry_id)
    await hass.async_block_till_done()
    assert list(second.runtime_data.coordinators) == ["laundry"]
    assert forgotten(hass).entity_id == entity_id
    assert hass.states.get(entity_id).state == "0.81"


async def test_yaml_moves_on_when_its_entry_fails_to_reload(
    hass: HomeAssistant, fake_client: MagicMock, pair: tuple[MockConfigEntry, MockConfigEntry]
) -> None:
    first, second = pair
    await start(hass)
    failures = [JevConnectionError("no route to host")]

    def probe_fails_once(state: Any, questions: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        if "probe" not in questions:
            return DEFAULT
        if failures:
            raise failures.pop()
        return build_response(input_tokens=PROBE_TOKENS, probe=NoulAnswer(noul=0.97))

    fake_client.ask.side_effect = probe_fails_once
    assert not await hass.config_entries.async_reload(first.entry_id)
    await hass.async_block_till_done()
    assert first.state is ConfigEntryState.SETUP_RETRY
    assert list(second.runtime_data.coordinators) == ["laundry"]
    assert hass.states.get(forgotten(hass).entity_id).state == "0.81"


async def test_yaml_skips_an_entry_that_failed_before_setup(
    hass: HomeAssistant, fake_client: MagicMock
) -> None:
    fake_client.ask.return_value = build_response(**laundry_answers())
    newer = MockConfigEntry(domain=DOMAIN, title="newer", data=ENTRY_DATA["typesafe"], version=3)
    second = MockConfigEntry(domain=DOMAIN, title="second", data=ENTRY_DATA["generic"])
    newer.add_to_hass(hass)
    second.add_to_hass(hass)
    hass.states.async_set(WASHER, "1.2")
    await start(hass)
    assert newer.state is ConfigEntryState.MIGRATION_ERROR
    assert list(second.runtime_data.coordinators) == ["laundry"]


async def test_yaml_stays_put_when_another_entry_goes(
    hass: HomeAssistant, pair: tuple[MockConfigEntry, MockConfigEntry]
) -> None:
    first, second = pair
    await start(hass)
    await hass.config_entries.async_remove(second.entry_id)
    await hass.async_block_till_done()
    assert list(first.runtime_data.coordinators) == ["laundry"]
    assert hass.data[YAML_WATCHES].holder == first.entry_id


async def test_yaml_of_a_lone_entry_waits_while_it_is_disabled(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    fake_client.ask.return_value = build_response(**laundry_answers())
    entry = await yaml_watch()
    yaml = hass.data[YAML_WATCHES]
    for disabled_by in (ConfigEntryDisabler.USER, None, ConfigEntryDisabler.USER):
        assert await hass.config_entries.async_set_disabled_by(entry.entry_id, disabled_by)
        await hass.async_block_till_done()
        assert yaml.holder == (None if disabled_by else entry.entry_id)
    # Enabled again, it ran them again. Removed while disabled, it leaves them to nobody.
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert yaml.holder is None


async def test_yaml_waits_for_the_oldest_entry_while_it_loads(
    hass: HomeAssistant, fake_client: MagicMock, pair: tuple[MockConfigEntry, MockConfigEntry]
) -> None:
    """At start the oldest entry may still be probing when newer ones are done."""
    first, second = pair
    third = MockConfigEntry(domain=DOMAIN, title="third", data=ENTRY_DATA["typesafe"])
    third.add_to_hass(hass)
    release = asyncio.Event()
    probes = 0

    async def answer(state: Any, questions: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        """The first probe waits for the test, and the third is refused."""
        nonlocal probes
        if "probe" not in questions:
            return build_response(**laundry_answers())
        probes += 1
        if probes == 1:
            await release.wait()
        elif probes == 3:
            raise JevConnectionError("no route to host")
        return build_response(input_tokens=PROBE_TOKENS, probe=NoulAnswer(noul=0.97))

    fake_client.ask.side_effect = answer
    setup = hass.async_create_task(async_setup_component(hass, DOMAIN, {DOMAIN: [WATCH]}))
    for _ in range(100):
        if second.state is ConfigEntryState.LOADED and third.state is ConfigEntryState.SETUP_RETRY:
            break
        await asyncio.sleep(0)
    assert first.state is ConfigEntryState.SETUP_IN_PROGRESS
    assert second.runtime_data.coordinators == {}
    release.set()
    assert await setup
    await hass.async_block_till_done()
    assert list(first.runtime_data.coordinators) == ["laundry"]
    assert hass.data[YAML_WATCHES].holder == first.entry_id
