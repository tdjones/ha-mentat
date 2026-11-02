"""One coordinator per watch: when it asks, what it sends, and when it refuses."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from jevclient import JevAuthError, JevConnectionError, JevRateLimitError, NoulAnswer
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.mentat.const import CONF_TOKEN_BUDGET, DOMAIN, EVENT_DECISION
from custom_components.mentat.coordinator import MentatCoordinator
from custom_components.mentat.watches import WatchConfig

from .conftest import (
    ANSWER_TOKENS,
    CHOICE_KEY,
    ENTRY_DATA,
    LATENCY_MS,
    NOUL_KEY,
    PROBE_TOKENS,
    SCORE_KEY,
    WASHER,
    WATCH,
    LoadYaml,
    build_response,
    laundry_answers,
)

FORGOTTEN = "sensor.typesafe_laundry_forgotten"
OVER_BUDGET = "binary_sensor.typesafe_over_budget"


def laundry(entry: MockConfigEntry) -> MentatCoordinator:
    return entry.runtime_data.coordinators["laundry"]


def asked(fake_client: MagicMock) -> int:
    """Requests other than the setup probe."""
    return sum("probe" not in call.args[1] for call in fake_client.ask.await_args_list)


async def advance(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


def tight_budget() -> MockConfigEntry:
    """An entry whose budget fits the probe and nothing after it."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="typesafe",
        data=ENTRY_DATA["typesafe"],
        options={CONF_TOKEN_BUDGET: 90},
    )


def disable_laundry(
    hass: HomeAssistant, entry: MockConfigEntry, platform: str | None = None
) -> None:
    """Disable the laundry watch's entities, or only those on one platform."""
    registry = er.async_get(hass)
    for registered in er.async_entries_for_config_entry(registry, entry.entry_id):
        if platform not in (None, registered.domain):
            continue
        if registered.unique_id.startswith(("yaml:laundry", "yaml:watch:laundry")):
            registry.async_update_entity(
                registered.entity_id, disabled_by=er.RegistryEntryDisabler.USER
            )


@pytest.fixture(autouse=True)
def answers(fake_client: MagicMock) -> None:
    fake_client.ask.return_value = build_response(**laundry_answers())


# --- the schedule and the triggers ---


async def test_coordinator_setup_asks_every_question_then_again_on_schedule(
    hass: HomeAssistant,
    fake_client: MagicMock,
    yaml_watch: LoadYaml,
    freezer: FrozenDateTimeFactory,
) -> None:
    # Frozen, so the two evaluations build the same state: `now` and each
    # record's age would otherwise move between them.
    freezer.move_to("2026-11-01 08:30:05+00:00")
    entry = await yaml_watch()
    assert asked(fake_client) == 1
    state, questions = fake_client.ask.await_args.args
    assert set(questions) == {NOUL_KEY, CHOICE_KEY, SCORE_KEY}
    assert state["entities"][0]["entity_id"] == WASHER

    await advance(hass, 302)
    assert asked(fake_client) == 2
    assert entry.runtime_data.ledger.tokens == PROBE_TOKENS + 2 * ANSWER_TOKENS
    last = laundry(entry).last
    assert last is not None
    assert last.latency_ms == LATENCY_MS
    assert last.body_bytes > 0
    assert last.state == state


@pytest.mark.parametrize("seconds", [2, 7])
async def test_coordinator_interval_is_the_scan_interval(
    hass: HomeAssistant, loaded: MockConfigEntry, seconds: int
) -> None:
    watch = WatchConfig(key="quick", name="Quick", questions=(), scan_interval=seconds)
    coordinator = MentatCoordinator(hass, loaded, loaded.runtime_data, watch)
    assert coordinator.update_interval == timedelta(seconds=seconds)


async def test_coordinator_changes_within_a_second_are_one_request(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    await yaml_watch()
    for reading in ("3.1", "3.2", "3.3"):
        hass.states.async_set(WASHER, reading)
        await hass.async_block_till_done()
    assert asked(fake_client) == 1
    await advance(hass, 1.4)
    assert asked(fake_client) == 2
    assert fake_client.ask.await_args.args[0]["entities"][0]["state"] == "3.3"


class HeldServer:
    """A server that holds every evaluation until `release` is set.

    `peak` is the most evaluations ever in flight at once. Each answer's noul
    comes from `nouls` in turn, so the order the answers land in shows.
    """

    def __init__(self, nouls: list[float] | None = None) -> None:
        self.release = asyncio.Event()
        self.release.set()
        self.asked = 0
        self.in_flight = 0
        self.peak = 0
        self.nouls = list(nouls or [])

    async def ask(self, state: Any, questions: dict[str, Any], *args: Any, **kwargs: Any):
        if "probe" in questions:
            return build_response(input_tokens=PROBE_TOKENS, probe=NoulAnswer(0.97))
        self.asked += 1
        noul = self.nouls.pop(0) if self.nouls else 0.81
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await self.release.wait()
        finally:
            self.in_flight -= 1
        return build_response(**{**laundry_answers(), NOUL_KEY: NoulAnswer(noul)})


@pytest.fixture
def held(fake_client: MagicMock) -> HeldServer:
    server = HeldServer(nouls=[0.15, 0.35, 0.95])
    fake_client.ask.side_effect = server.ask
    return server


async def test_coordinator_requests_during_an_evaluation_fold_into_one_more(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml, held: HeldServer
) -> None:
    events = async_capture_events(hass, EVENT_DECISION)
    await yaml_watch()
    assert held.asked == 1
    held.release.clear()

    hass.states.async_set(WASHER, "110")
    await advance(hass, 1.4)
    assert (held.asked, held.in_flight) == (2, 1)
    # Two more changes while the server is still thinking.
    hass.states.async_set(WASHER, "220")
    await advance(hass, 2.8)
    hass.states.async_set(WASHER, "330")
    await advance(hass, 4.2)
    assert held.asked == 2

    held.release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert (held.asked, held.peak) == (3, 1)
    # The follow-up read the last change, and the answers landed in order.
    assert fake_client.ask.await_args.args[0]["entities"][0]["state"] == "330"
    assert [event.data["answers"][NOUL_KEY]["noul"] for event in events] == [0.15, 0.35, 0.95]
    assert hass.states.get(FORGOTTEN).state == "0.95"
    await advance(hass, 12)
    assert held.asked == 3


async def test_coordinator_schedule_counts_from_the_answer_not_missed_ticks(
    hass: HomeAssistant, yaml_watch: LoadYaml, held: HeldServer
) -> None:
    await yaml_watch()
    held.release.clear()
    hass.states.async_set(WASHER, "110")
    await advance(hass, 1.4)
    assert held.in_flight == 1
    # The schedule comes due twice while that evaluation is out.
    await advance(hass, 302)
    await advance(hass, 604)
    assert held.asked == 2

    held.release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert held.asked == 2
    await advance(hass, 302)
    assert (held.asked, held.peak) == (3, 1)


async def test_coordinator_ticks_during_an_evaluation_become_one_follow_up(
    hass: HomeAssistant, yaml_watch: LoadYaml, held: HeldServer
) -> None:
    entry = await yaml_watch()
    held.release.clear()
    hass.states.async_set(WASHER, "110")
    await advance(hass, 1.4)
    assert held.in_flight == 1
    # Two ticks already on their way when the evaluation started.
    await laundry(entry)._handle_refresh_interval()
    await laundry(entry)._handle_refresh_interval()
    assert held.asked == 2

    held.release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert (held.asked, held.peak) == (3, 1)


async def test_coordinator_change_during_a_scheduled_evaluation_is_kept(
    hass: HomeAssistant, yaml_watch: LoadYaml, held: HeldServer
) -> None:
    await yaml_watch()
    held.release.clear()
    await advance(hass, 302)
    assert held.in_flight == 1
    hass.states.async_set(WASHER, "110")
    await advance(hass, 304)
    held.release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert (held.asked, held.peak) == (3, 1)


async def test_coordinator_stopping_home_assistant_skips_the_tick(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    await yaml_watch()
    hass.set_state(CoreState.stopping)
    try:
        await advance(hass, 302)
    finally:
        hass.set_state(CoreState.running)
    assert asked(fake_client) == 1


async def test_coordinator_wake_on_replaces_the_target_as_trigger(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    hass.states.async_set("binary_sensor.lid", "off")
    await yaml_watch([{**WATCH, "wake_on": ["binary_sensor.lid"]}])
    hass.states.async_set(WASHER, "12")
    await advance(hass, 5)
    assert asked(fake_client) == 1
    hass.states.async_set("binary_sensor.lid", "on")
    await advance(hass, 5)
    assert asked(fake_client) == 2


async def test_coordinator_note_only_watch_sends_the_note_and_tracks_nothing(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    plain = {key: value for key, value in WATCH.items() if key != "entities"}
    entry = await yaml_watch([{**plain, "note": "The drum has stopped."}])
    assert fake_client.ask.await_args.args[0] == "The drum has stopped."
    assert laundry(entry)._debouncer is None


async def test_coordinator_unload_stops_the_triggers_and_twice_is_harmless(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    entry = await yaml_watch()
    coordinator = laundry(entry)
    hass.states.async_set(WASHER, "6")
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    hass.states.async_set(WASHER, "9")
    await advance(hass, 75)
    assert asked(fake_client) == 1
    coordinator.async_shutdown_triggers()


def other_entrys_answer(hass: HomeAssistant) -> str:
    """A sensor another Mentat entry owns, with a state."""
    registered = er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, "elsewhere_answer", suggested_object_id="neighbour_answer"
    )
    hass.states.async_set(registered.entity_id, "0.45")
    return registered.entity_id


@pytest.mark.parametrize("field", ["entities", "wake_on"])
async def test_coordinator_mentat_answers_never_wake_the_watch(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml, field: str
) -> None:
    neighbour = other_entrys_answer(hass)
    await yaml_watch([{**WATCH, field: [WASHER, FORGOTTEN, neighbour]}])
    assert asked(fake_client) == 1

    hass.states.async_set(FORGOTTEN, "0.55")
    hass.states.async_set(neighbour, "0.65")
    await advance(hass, 4)
    assert asked(fake_client) == 1
    hass.states.async_set(WASHER, "120")
    await advance(hass, 8)
    assert asked(fake_client) == 2
    if field == "entities":
        # The target picks the answer, so it is still read as part of the state.
        sent = fake_client.ask.await_args.args[0]["entities"]
        assert FORGOTTEN in [record["entity_id"] for record in sent]


# --- who reads the answers ---


async def test_coordinator_watch_with_every_entity_disabled_is_never_asked(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    entry = await yaml_watch([{**WATCH, "fire_event": False}])
    disable_laundry(hass, entry)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert asked(fake_client) == 1
    assert laundry(entry).nobody_reads()
    hass.states.async_set(WASHER, "4")
    await advance(hass, 12)
    await advance(hass, 302)
    assert asked(fake_client) == 1


async def test_coordinator_watch_firing_events_is_read_with_entities_disabled(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    entry = await yaml_watch()
    disable_laundry(hass, entry)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    events = async_capture_events(hass, EVENT_DECISION)
    # An automation on mentat_decision reads it with no entity enabled.
    assert not laundry(entry).nobody_reads()
    assert asked(fake_client) == 2
    hass.states.async_set(WASHER, "4")
    await advance(hass, 2)
    assert asked(fake_client) == 3
    # The schedule keeps running too.
    await advance(hass, 304)
    assert asked(fake_client) == 4
    assert len(events) == 2


async def test_coordinator_enabled_verdict_alone_is_a_reader(
    hass: HomeAssistant, yaml_watch: LoadYaml
) -> None:
    entry = await yaml_watch([{**WATCH, "fire_event": False}])
    disable_laundry(hass, entry, platform="sensor")
    assert not laundry(entry).nobody_reads()


# --- the budget and the server's errors ---


async def test_coordinator_request_over_budget_is_never_sent(
    hass: HomeAssistant,
    fake_client: MagicMock,
    yaml_watch: LoadYaml,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = await yaml_watch(owner=tight_budget())
    assert asked(fake_client) == 0
    ledger = entry.runtime_data.ledger
    assert ledger.over_budget
    assert ledger.tokens == PROBE_TOKENS
    issue = ir.async_get(hass).async_get_issue(DOMAIN, ledger.issue_id)
    assert issue is not None
    assert issue.translation_key == "over_budget"
    assert hass.states.get(FORGOTTEN).state == STATE_UNAVAILABLE
    assert hass.states.get(OVER_BUDGET).state == "on"
    error = laundry(entry).last_exception
    assert error.translation_key == "watch_over_budget"
    assert error.translation_placeholders["watch"] == "Laundry"
    assert caplog.text.count("Stopped evaluating") == 1

    # The next interval is refused as well, and not logged again.
    await advance(hass, 302)
    assert asked(fake_client) == 0
    assert caplog.text.count("Stopped evaluating") == 1


async def test_coordinator_refused_evaluation_does_not_wake_its_own_watch(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    # The watch reads its own answer, so a refusal that changed it could wake
    # the watch again.
    entry = await yaml_watch([{**WATCH, "entities": [WASHER, FORGOTTEN]}], owner=tight_budget())
    coordinator = laundry(entry)
    with patch.object(
        coordinator, "_async_update_data", wraps=coordinator._async_update_data
    ) as update:
        hass.states.async_set(WASHER, "110")
        await advance(hass, 1.4)
        assert update.await_count == 1
        await advance(hass, 40)
        assert update.await_count == 1
    assert coordinator.last_exception.translation_key == "watch_over_budget"
    assert asked(fake_client) == 0


async def test_coordinator_rate_limit_waits_the_servers_retry_after(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    entry = await yaml_watch()
    fake_client.ask.side_effect = JevRateLimitError("easy now", retry_after=1_200)
    await advance(hass, 302)
    assert asked(fake_client) == 2
    error = laundry(entry).last_exception
    assert error.translation_key == "rate_limited"
    assert error.retry_after == 1_200
    # The scan interval alone would have asked again by now.
    await advance(hass, 302 + 600)
    assert asked(fake_client) == 2
    await advance(hass, 302 + 1_210)
    assert asked(fake_client) == 3


async def test_coordinator_refused_key_starts_reauth(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    await yaml_watch()
    fake_client.ask.side_effect = JevAuthError("revoked", status=403)
    await advance(hass, 302)
    progress = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in progress] == [SOURCE_REAUTH]


async def test_coordinator_outage_and_recovery_are_logged_once_each(
    hass: HomeAssistant,
    fake_client: MagicMock,
    yaml_watch: LoadYaml,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = await yaml_watch()
    caplog.set_level(logging.INFO, logger="custom_components.mentat.coordinator")
    fake_client.ask.side_effect = JevConnectionError("connection reset")
    for step in (1, 2, 3):
        await advance(hass, 302 * step)
    assert caplog.text.count("is not answering") == 1
    assert hass.states.get(FORGOTTEN).state == STATE_UNAVAILABLE
    assert laundry(entry).last_exception.translation_key == "ask_failed"

    fake_client.ask.side_effect = None
    for step in (4, 5):
        await advance(hass, 302 * step)
    assert caplog.text.count("is answering again") == 1
    assert hass.states.get(FORGOTTEN).state == "0.81"


@pytest.mark.parametrize(
    ("change", "key"),
    [
        ({"note": "{{ 7 / 0 }}"}, "note_failed"),
        ({"entities": {"area_id": "attic"}}, "watch_target_failed"),
    ],
)
async def test_coordinator_broken_state_names_the_watch(
    hass: HomeAssistant,
    fake_client: MagicMock,
    yaml_watch: LoadYaml,
    change: dict[str, Any],
    key: str,
) -> None:
    entry = await yaml_watch([{**WATCH, **change}])
    assert asked(fake_client) == 0
    error = laundry(entry).last_exception
    assert error.translation_key == key
    assert error.translation_placeholders["watch"] == "Laundry"


# --- the decision event ---


async def test_coordinator_event_carries_every_answer(
    hass: HomeAssistant, yaml_watch: LoadYaml
) -> None:
    events = async_capture_events(hass, EVENT_DECISION)
    entry = await yaml_watch()
    [event] = events
    assert event.data == {
        "entry_id": entry.entry_id,
        "watch": "laundry",
        "watch_name": "Laundry",
        "answers": {
            NOUL_KEY: {
                "type": "noul",
                "noul": 0.81,
                "name": "Laundry forgotten",
                "verdict": True,
            },
            CHOICE_KEY: {
                "type": "choice",
                "choice": "spin",
                "confidence": 0.8,
                "probabilities": {"wash": 0.1, "spin": 0.85, "idle": 0.05},
                "name": "Programme",
            },
            SCORE_KEY: event.data["answers"][SCORE_KEY],
        },
        "window": None,
        "model": "jev-1.14.0",
        "latency_ms": LATENCY_MS,
        "input_tokens": ANSWER_TOKENS,
    }
    assert event.data["answers"][SCORE_KEY]["nearest_level"] == "Today"
    assert event.data["answers"][SCORE_KEY]["name"] == "Urgency"


async def test_coordinator_event_leaves_out_a_missing_answer(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    fake_client.ask.return_value = build_response(**{NOUL_KEY: NoulAnswer(noul=0.25)})
    events = async_capture_events(hass, EVENT_DECISION)
    await yaml_watch()
    answers = events[0].data["answers"]
    assert list(answers) == [NOUL_KEY]
    assert answers[NOUL_KEY]["verdict"] is False
    assert hass.states.get("sensor.typesafe_programme").state == STATE_UNAVAILABLE


async def test_coordinator_fire_event_false_keeps_the_bus_quiet(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    events = async_capture_events(hass, EVENT_DECISION)
    await yaml_watch([{**WATCH, "fire_event": False}])
    assert asked(fake_client) == 1
    assert events == []
