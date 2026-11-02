"""Setting an entry up and down: the probe, the ledger's store, midnight and migration."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from jevclient import JevAuthError, JevConnectionError, NoulAnswer
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.mentat import YAML_WATCHES, async_migrate_entry, async_unload_entry
from custom_components.mentat.const import (
    CONF_ACCOUNT_ID,
    CONF_TOKEN_BUDGET,
    CONF_TOKEN_PRICE,
    DOMAIN,
)
from custom_components.mentat.coordinator import MentatRuntimeData
from custom_components.mentat.entity import build_device_info

from .conftest import (
    ENTRY_DATA,
    PROBE_TOKENS,
    WASHER,
    WATCH,
    FakeRecorder,
    LoadYaml,
    build_response,
)

ALL_PROVIDERS = ["typesafe", "openrouter", "cloudflare", "local_clef", "generic"]


def store_key(entry: MockConfigEntry) -> str:
    return f"{DOMAIN}.{entry.entry_id}.ledger"


def stored_day(day: str, requests: int, tokens: int) -> dict[str, Any]:
    """The ledger store's file, the way Store writes it."""
    return {
        "version": 1,
        "minor_version": 1,
        "key": "unused",
        "data": {"day": day, "requests": requests, "tokens": tokens},
    }


def today() -> str:
    return dt_util.now().date().isoformat()


@pytest.mark.parametrize("provider_key", ALL_PROVIDERS, indirect=True)
async def test_setup_every_provider_probes_then_unload_writes_the_ledger(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    fake_client: MagicMock,
    entry: MockConfigEntry,
    provider_key: str,
) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    runtime: MentatRuntimeData = entry.runtime_data
    assert runtime.client is fake_client
    assert runtime.provider.key == provider_key
    assert runtime.model == ENTRY_DATA[provider_key]["model"]
    assert runtime.model_version == "jev-1.14.0"
    assert runtime.coordinators == {}
    # The probe is billed, so the ledger counts it.
    assert (runtime.ledger.requests, runtime.ledger.tokens) == (1, PROBE_TOKENS)

    built = fake_client.built_by_setup.call_args.kwargs
    assert built["api_key"] == ENTRY_DATA[provider_key][CONF_API_KEY]
    assert built["account_id"] == ENTRY_DATA[provider_key].get(CONF_ACCOUNT_ID)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    fake_client.async_close.assert_awaited()
    # Written at unload, not after the save delay.
    assert hass_storage[store_key(entry)]["data"]["requests"] == 1


async def test_setup_refused_key_is_an_error_that_starts_reauth(
    hass: HomeAssistant, fake_client: MagicMock, entry: MockConfigEntry
) -> None:
    fake_client.ask.side_effect = JevAuthError("key revoked by the owner", status=401)
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    progress = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in progress] == [SOURCE_REAUTH]
    assert entry.reason is not None
    assert "TypeSafe Jev" in entry.reason
    assert "key revoked by the owner" in entry.reason


async def test_setup_unreachable_server_is_retried(
    hass: HomeAssistant, fake_client: MagicMock, entry: MockConfigEntry
) -> None:
    fake_client.ask.side_effect = JevConnectionError("no route to host")
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry.reason is not None
    assert "TypeSafe Jev" in entry.reason
    assert "no route to host" in entry.reason


async def test_setup_restores_todays_ledger_and_adds_the_probe(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    fake_client: MagicMock,
    entry: MockConfigEntry,
) -> None:
    hass_storage[store_key(entry)] = stored_day(today(), 9, 2_400)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    ledger = entry.runtime_data.ledger
    assert (ledger.requests, ledger.tokens) == (10, 2_400 + PROBE_TOKENS)


async def test_setup_drops_yesterdays_ledger(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    fake_client: MagicMock,
    entry: MockConfigEntry,
) -> None:
    yesterday = dt_util.now().date() - timedelta(days=1)
    hass_storage[store_key(entry)] = stored_day(yesterday.isoformat(), 9, 2_400)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    ledger = entry.runtime_data.ledger
    assert (ledger.requests, ledger.tokens) == (1, PROBE_TOKENS)


async def test_setup_spent_budget_skips_the_probe_until_the_budget_rises(
    hass: HomeAssistant, hass_storage: dict[str, Any], fake_client: MagicMock
) -> None:
    spent = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA["typesafe"], options={CONF_TOKEN_BUDGET: 1_200}
    )
    hass_storage[store_key(spent)] = stored_day(today(), 4, 1_800)
    spent.add_to_hass(hass)
    assert await hass.config_entries.async_setup(spent.entry_id)
    await hass.async_block_till_done()

    assert spent.state is ConfigEntryState.LOADED
    assert fake_client.ask.await_count == 0
    ledger = spent.runtime_data.ledger
    assert ledger.over_budget
    issues = ir.async_get(hass)
    issue = issues.async_get_issue(DOMAIN, ledger.issue_id)
    assert issue is not None
    assert issue.translation_placeholders == {"budget": "1200", "used": "1800"}

    # Raising the budget reloads the entry, which clears the flag and the issue.
    hass.config_entries.async_update_entry(spent, options={CONF_TOKEN_BUDGET: 12_000})
    await hass.async_block_till_done()
    assert spent.state is ConfigEntryState.LOADED
    assert not spent.runtime_data.ledger.over_budget
    assert issues.async_get_issue(DOMAIN, ledger.issue_id) is None
    assert fake_client.ask.await_count == 1


async def test_setup_midnight_zeroes_the_day_without_a_request(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    fake_client: MagicMock,
    entry: MockConfigEntry,
) -> None:
    freezer.move_to(dt_util.start_of_local_day() + timedelta(hours=23, minutes=58))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    ledger = entry.runtime_data.ledger
    heard: list[int] = []
    stop = ledger.async_add_listener(lambda: heard.append(ledger.tokens))
    assert ledger.requests == 1

    midnight = dt_util.start_of_local_day() + timedelta(days=1)
    freezer.move_to(midnight)
    async_fire_time_changed(hass, midnight)
    await hass.async_block_till_done()

    assert ledger.day == midnight.date()
    assert (ledger.requests, ledger.tokens) == (0, 0)
    assert heard == [0]
    assert fake_client.ask.await_count == 1
    stop()
    ledger.notify()
    assert heard == [0]


async def test_setup_options_change_reloads_with_the_new_price(
    hass: HomeAssistant, fake_client: MagicMock, loaded: MockConfigEntry
) -> None:
    assert loaded.runtime_data.ledger.token_price == 0.042
    hass.config_entries.async_update_entry(loaded, options={CONF_TOKEN_PRICE: 0.15})
    await hass.async_block_till_done()
    assert loaded.runtime_data.ledger.token_price == 0.15
    assert fake_client.ask.await_count == 2


async def test_setup_keeps_yaml_as_written_for_its_entry(hass: HomeAssistant) -> None:
    watches = [
        {
            "name": "Night",
            "note": "lights out",
            "questions": [{"name": "Asleep", "type": "noul", "instructions": "Asleep?"}],
        }
    ]
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: watches})
    [kept] = hass.data[YAML_WATCHES].raw
    assert kept["name"] == "Night"
    # Defaults are filled in when the watch is built, so a window can tell an
    # interval the user wrote from one they did not.
    assert "scan_interval" not in kept


async def test_setup_entry_from_a_newer_version_is_a_migration_error(
    hass: HomeAssistant, fake_client: MagicMock
) -> None:
    newer = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA["typesafe"], version=3)
    newer.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(newer.entry_id)
    assert newer.state is ConfigEntryState.MIGRATION_ERROR


async def test_setup_migration_accepts_a_current_entry(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    assert await async_migrate_entry(hass, entry)


async def test_setup_probe_without_a_model_leaves_the_version_unknown(
    hass: HomeAssistant, fake_client: MagicMock, entry: MockConfigEntry
) -> None:
    fake_client.ask.side_effect = None
    fake_client.ask.return_value = build_response(model="", probe=NoulAnswer(noul=0.99))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.runtime_data.model_version is None


def test_setup_device_is_the_entry_made_by_its_provider(entry: MockConfigEntry) -> None:
    device = build_device_info(entry)
    assert device["identifiers"] == {(DOMAIN, entry.entry_id)}
    assert device["manufacturer"] == "TypeSafe Jev"
    assert device["model"] == "jev-latest"
    assert device["name"] == entry.title


async def test_setup_failed_platform_unload_keeps_the_entry_running(
    hass: HomeAssistant, fake_client: MagicMock, loaded: MockConfigEntry
) -> None:
    with patch.object(hass.config_entries, "async_unload_platforms", return_value=False):
        assert not await async_unload_entry(hass, loaded)
    fake_client.async_close.assert_not_awaited()
    assert await hass.config_entries.async_unload(loaded.entry_id)
    fake_client.async_close.assert_awaited_once()


# --- failure, removal and start ---


def evaluations(fake_client: MagicMock) -> int:
    """Requests other than the setup probe."""
    return sum("probe" not in call.args[1] for call in fake_client.ask.await_args_list)


async def test_setup_that_fails_after_a_watch_started_stops_it(
    hass: HomeAssistant, fake_client: MagicMock, entry: MockConfigEntry
) -> None:
    hass.states.async_set(WASHER, "1.2")
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: [WATCH]})
    entry.add_to_hass(hass)
    broken = RuntimeError("platform broke")
    with patch.object(hass.config_entries, "async_forward_entry_setups", side_effect=broken):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert evaluations(fake_client) == 1
    # A failed entry is billed for nothing: not on a change, not on the schedule.
    hass.states.async_set(WASHER, "1380")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=5))
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=WATCH["scan_interval"] + 5))
    await hass.async_block_till_done()
    assert evaluations(fake_client) == 1
    fake_client.async_close.assert_awaited_once()


async def test_setup_that_fails_reading_the_recorder_leaves_no_listener(
    hass: HomeAssistant,
    fake_client: MagicMock,
    entry: MockConfigEntry,
    mock_recorder: FakeRecorder,
) -> None:
    hass.states.async_set(WASHER, "1.2")
    window = {"duration": "00:30:00", "events": [{"event_type": "doorbell_pressed"}]}
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: [{**WATCH, "window": window}]})
    entry.add_to_hass(hass)
    # Not a database error, so the window does not shrug it off.
    mock_recorder.error = RuntimeError("disk gone")
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert "doorbell_pressed" not in hass.bus.async_listeners()


async def test_setup_starts_every_watch_at_once(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    """Each first request waits for the other. Asked in turn, the first would wait alone."""
    arrived: list[str] = []
    both = asyncio.Event()

    async def answer(state: Any, questions: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        if "probe" in questions:
            return build_response(input_tokens=PROBE_TOKENS, probe=NoulAnswer(noul=0.97))
        arrived.extend(questions)
        if len(arrived) == 2:
            both.set()
        await asyncio.wait_for(both.wait(), timeout=1)
        return build_response(**dict.fromkeys(questions, NoulAnswer(noul=0.4)))

    fake_client.ask.side_effect = answer
    one = {**WATCH, "questions": WATCH["questions"][:1]}
    await yaml_watch([one, {**one, "name": "Dryer"}])
    assert sorted(arrived) == ["dryer_laundry_forgotten", "laundry_laundry_forgotten"]
    assert hass.states.get("sensor.typesafe_laundry_forgotten").state == "0.4"
    assert hass.states.get("sensor.typesafe_laundry_forgotten_2").state == "0.4"


async def test_setup_removing_the_entry_deletes_its_ledger_and_its_issue(
    hass: HomeAssistant, hass_storage: dict[str, Any], fake_client: MagicMock
) -> None:
    spent = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA["typesafe"], options={CONF_TOKEN_BUDGET: 1_200}
    )
    hass_storage[store_key(spent)] = stored_day(today(), 4, 1_800)
    spent.add_to_hass(hass)
    assert await hass.config_entries.async_setup(spent.entry_id)
    await hass.async_block_till_done()
    issues = ir.async_get(hass)
    issue_id = spent.runtime_data.ledger.issue_id
    assert issues.async_get_issue(DOMAIN, issue_id) is not None

    await hass.config_entries.async_remove(spent.entry_id)
    await hass.async_block_till_done()
    assert issues.async_get_issue(DOMAIN, issue_id) is None
    assert store_key(spent) not in hass_storage
