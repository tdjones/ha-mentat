"""A watch with a window: what its coordinator sends, when it asks, and what it shows."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.mentat.const import DOMAIN, EVENT_DECISION
from custom_components.mentat.diagnostics import async_get_config_entry_diagnostics

from .conftest import (
    WASHER,
    WATCH,
    FakeRecorder,
    LoadYaml,
    build_response,
    laundry_answers,
)

FORGOTTEN = "sensor.typesafe_laundry_forgotten"
FORGOTTEN_VERDICT = "binary_sensor.typesafe_laundry_forgotten"
WINDOW_KEYS = {"window_start", "window_end", "event_count", "last_evaluated"}


def windowed(**window: Any) -> dict[str, Any]:
    """WATCH with a 45 minute window that settles 8 s after a change."""
    return {**WATCH, "window": {"duration": "00:45:00", "settle": 8, **window}}


def asked(fake_client: MagicMock) -> int:
    return sum("probe" not in call.args[1] for call in fake_client.ask.await_args_list)


async def advance(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


@pytest.fixture(autouse=True)
def answers(fake_client: MagicMock) -> None:
    fake_client.ask.return_value = build_response(**laundry_answers())


async def test_window_watch_sends_its_log_with_the_target(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    entry = await yaml_watch([windowed()])
    window = entry.runtime_data.coordinators["laundry"].window
    assert window is not None
    assert window.entity_ids == [WASHER]
    state = fake_client.ask.await_args.args[0]
    assert state["window"]["minutes"] == 45
    assert state["events"] == []
    [record] = state["entities"]
    assert record["entity_id"] == WASHER
    assert record["changes_in_window"] == 0


async def test_window_watch_change_during_the_backfill_is_asked_about_once(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml, mock_recorder: FakeRecorder
) -> None:
    """The request at start reads the change. A settle wait after it would bill it again."""
    mock_recorder.during = lambda: hass.states.async_set(WASHER, "1380")
    await yaml_watch([windowed(evaluate="on_event")])
    await advance(hass, 30)
    assert asked(fake_client) == 1
    events = fake_client.ask.await_args.args[0]["events"]
    assert [(event["from"], event["to"]) for event in events] == [("1.2", "1380")]


async def test_window_watch_on_event_asks_once_settled_and_never_on_schedule(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    entry = await yaml_watch([windowed(evaluate="on_event")])
    assert entry.runtime_data.coordinators["laundry"].update_interval is None
    assert asked(fake_client) == 1
    hass.states.async_set(WASHER, "1380")
    await advance(hass, 4)
    assert asked(fake_client) == 1
    await advance(hass, 9)
    assert asked(fake_client) == 2
    events = fake_client.ask.await_args.args[0]["events"]
    assert [(event["from"], event["to"]) for event in events] == [("1.2", "1380")]
    await advance(hass, 7_200)
    assert asked(fake_client) == 2


async def test_window_watch_interval_logs_changes_and_asks_on_schedule_only(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    entry = await yaml_watch([windowed(evaluate="interval")])
    assert entry.runtime_data.coordinators["laundry"].update_interval == timedelta(seconds=300)
    hass.states.async_set(WASHER, "1380")
    await advance(hass, 70)
    assert asked(fake_client) == 1
    await advance(hass, 302)
    assert asked(fake_client) == 2
    assert len(fake_client.ask.await_args.args[0]["events"]) == 1


async def test_window_watch_both_asks_once_settled_and_on_schedule(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    entry = await yaml_watch([windowed(evaluate="both")])
    assert entry.runtime_data.coordinators["laundry"].update_interval == timedelta(seconds=300)
    hass.states.async_set(WASHER, "1380")
    await advance(hass, 9)
    assert asked(fake_client) == 2
    await advance(hass, 315)
    assert asked(fake_client) == 3


async def test_window_watch_settling_with_no_reader_does_not_ask(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    entry = await yaml_watch([{**windowed(evaluate="on_event"), "fire_event": False}])
    registry = er.async_get(hass)
    for registered in er.async_entries_for_config_entry(registry, entry.entry_id):
        registry.async_update_entity(
            registered.entity_id, disabled_by=er.RegistryEntryDisabler.USER
        )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    before = asked(fake_client)
    hass.states.async_set(WASHER, "1380")
    await advance(hass, 70)
    assert asked(fake_client) == before


async def test_window_watch_event_says_which_window_was_judged(
    hass: HomeAssistant, yaml_watch: LoadYaml
) -> None:
    events = async_capture_events(hass, EVENT_DECISION)
    await yaml_watch([windowed(evaluate="on_event")])
    hass.states.async_set(WASHER, "1380")
    await advance(hass, 9)
    first, second = (event.data["window"] for event in events)
    assert (first["events"], second["events"]) == (0, 1)
    start = dt_util.parse_datetime(second["start"])
    end = dt_util.parse_datetime(second["end"])
    assert start is not None
    assert end is not None
    assert end - start == timedelta(minutes=45)


async def test_window_watch_entities_say_which_window_they_answer(
    hass: HomeAssistant, yaml_watch: LoadYaml
) -> None:
    entry = await yaml_watch([windowed()])
    span = entry.runtime_data.coordinators["laundry"].window_span
    assert span is not None
    for entity_id in (FORGOTTEN, FORGOTTEN_VERDICT):
        attributes = hass.states.get(entity_id).attributes
        assert set(attributes) >= WINDOW_KEYS
        assert attributes["event_count"] == 0
        assert attributes["window_end"] == span.end.isoformat()
    for entity_id in ("sensor.typesafe_programme", "sensor.typesafe_urgency"):
        assert set(hass.states.get(entity_id).attributes) >= WINDOW_KEYS


async def test_window_watch_attributes_absent_on_a_plain_watch(
    hass: HomeAssistant, yaml_watch: LoadYaml
) -> None:
    await yaml_watch()
    for entity_id in (FORGOTTEN, FORGOTTEN_VERDICT):
        assert not WINDOW_KEYS & set(hass.states.get(entity_id).attributes)


async def test_window_watch_unload_stops_the_log(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    entry = await yaml_watch([windowed(evaluate="on_event")])
    coordinator = entry.runtime_data.coordinators["laundry"]
    window = coordinator.window
    assert window is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert coordinator.window is None
    hass.states.async_set(WASHER, "1380")
    await advance(hass, 70)
    assert window.buffered == 0
    assert asked(fake_client) == 1


async def test_window_watch_over_the_entity_limit_names_the_limit(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("custom_components.mentat.window.MAX_WINDOW_ENTITIES", 0)
        entry = await yaml_watch([windowed()])
    assert asked(fake_client) == 0
    error = entry.runtime_data.coordinators["laundry"].last_exception
    assert error.translation_key == "watch_target_failed"
    assert "one request may carry 0" in error.translation_placeholders["reason"]


async def test_window_watch_diagnostics_count_the_log(
    hass: HomeAssistant, yaml_watch: LoadYaml
) -> None:
    entry = await yaml_watch([windowed(), {**WATCH, "name": "Spare"}])
    hass.states.async_set(WASHER, "1380")
    await hass.async_block_till_done()
    report = await async_get_config_entry_diagnostics(hass, entry)
    assert report["watches"] == ["laundry", "spare"]
    assert report["windows"] == {
        "laundry": {
            "evaluate": "both",
            "minutes": 45,
            "entities": 1,
            "event_filters": 0,
            "max_events": 100,
            "buffered": 1,
            "in_window": 1,
            "truncated": False,
            "last_evaluated_events": 0,
        }
    }


def other_entrys_answer(hass: HomeAssistant) -> str:
    registered = er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, "elsewhere_answer", suggested_object_id="neighbour_answer"
    )
    hass.states.async_set(registered.entity_id, "0.45")
    return registered.entity_id


@pytest.mark.parametrize("field", ["entities", "wake_on"])
async def test_window_watch_ignores_mentat_answers(
    hass: HomeAssistant,
    fake_client: MagicMock,
    yaml_watch: LoadYaml,
    mock_recorder: FakeRecorder,
    field: str,
) -> None:
    neighbour = other_entrys_answer(hass)
    watch = {**windowed(evaluate="on_event"), "entities": [WASHER, FORGOTTEN]}
    if field == "wake_on":
        watch["wake_on"] = [WASHER, FORGOTTEN, neighbour]
    entry = await yaml_watch([watch])
    assert asked(fake_client) == 1
    window = entry.runtime_data.coordinators["laundry"].window
    assert window is not None

    hass.states.async_set(FORGOTTEN, "0.55")
    hass.states.async_set(neighbour, "0.65")
    await advance(hass, 70)
    assert window.buffered == 0
    assert asked(fake_client) == 1
    hass.states.async_set(WASHER, "120")
    await advance(hass, 140)
    assert window.buffered == 1
    assert asked(fake_client) == 2


async def test_window_watch_backfill_skips_mentat_answers(
    hass: HomeAssistant, yaml_watch: LoadYaml, mock_recorder: FakeRecorder
) -> None:
    entry = await yaml_watch([{**windowed(), "entities": [WASHER, FORGOTTEN]}])
    # Registered by now, so the reload's backfill knows the sensor is Mentat's.
    mock_recorder.calls.clear()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert [call["entity_ids"] for call in mock_recorder.calls] == [[WASHER]]
