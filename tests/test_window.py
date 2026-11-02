"""The event log a window watch reads: what it keeps, what it drops, and when it
says the window has settled.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from sqlalchemy.exc import OperationalError

from custom_components.mentat.const import DOMAIN
from custom_components.mentat.readings import ago, span
from custom_components.mentat.watches import EventFilter, WatchConfig, WindowConfig
from custom_components.mentat.window import (
    EventWindow,
    WindowEntry,
    async_build_window_state,
    matches,
)

from .conftest import FakeRecorder

ALICE = "person.alice"
PHONE = "binary_sensor.alice_phone_charging"
MOTION = "binary_sensor.living_room_motion"
DOOR = "binary_sensor.front_door"
THERMOSTAT = "climate.hallway"
CAMERA = EventFilter(
    event_type="person_detected",
    data={"camera": "front"},
    label="front door camera saw a person",
)


def window_context(
    entities: tuple[str, ...] = (DOOR, MOTION),
    *,
    wake_on: tuple[str, ...] = (),
    minutes: int = 30,
    **window: Any,
) -> WatchConfig:
    return WatchConfig(
        key="window",
        name="Window",
        questions=(),
        scan_interval=300,
        target={"entity_id": list(entities)},
        wake_on=wake_on,
        window=WindowConfig(duration=timedelta(minutes=minutes), **window),
    )


async def started(
    hass: HomeAssistant,
    watch: WatchConfig,
    on_change: Any = None,
    *,
    live: bool = True,
) -> EventWindow:
    window = EventWindow(hass, watch, on_change)
    await window.async_start(live=live)
    return window


def at(clock: str) -> datetime:
    """A moment on 30 October 2026, UTC."""
    return datetime.fromisoformat(f"2026-10-30T{clock}+00:00")


async def move(hass: HomeAssistant, freezer: FrozenDateTimeFactory, clock: str) -> None:
    freezer.move_to(at(clock))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def lines(window: EventWindow) -> list[tuple[str | None, str | None, str | None]]:
    return [
        (entry.entity_id or entry.event_type, entry.old, entry.new)
        for entry in window.entries(dt_util.utcnow())
    ]


@pytest.fixture(autouse=True)
async def utc(hass: HomeAssistant) -> None:
    """Clock times in the expected JSON are UTC, whatever the host says."""
    await hass.config.async_set_time_zone("UTC")


# --- the log ---


async def test_changes_are_logged_oldest_first(hass: HomeAssistant) -> None:
    hass.states.async_set(DOOR, "off", {"friendly_name": "Front door"})
    hass.states.async_set(MOTION, "off")
    window = await started(hass, window_context())
    assert window.entity_ids == [DOOR, MOTION]
    hass.states.async_set(DOOR, "on", {"friendly_name": "Front door"})
    hass.states.async_set(MOTION, "on")
    await hass.async_block_till_done()
    assert lines(window) == [(DOOR, "off", "on"), (MOTION, "off", "on")]
    assert window.entries(dt_util.utcnow())[0].name == "Front door"
    window.async_stop()


async def test_the_buffer_keeps_the_newest_and_says_it_dropped_some(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await move(hass, freezer, "22:00:00")
    hass.states.async_set(DOOR, "0")
    window = await started(hass, window_context(max_events=10))
    for minute in range(1, 13):
        await move(hass, freezer, f"22:{minute:02d}:00")
        hass.states.async_set(DOOR, str(minute))
    await hass.async_block_till_done()
    assert window.buffered == 10
    assert [new for _, _, new in lines(window)] == [str(m) for m in range(3, 13)]
    assert window.truncated(dt_util.utcnow())
    # Once the dropped entries are older than the window, nothing in it is missing.
    assert not window.truncated(at("22:33:00"))
    window.async_stop()


async def test_entries_outside_the_window_are_not_read(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await move(hass, freezer, "22:00:00")
    hass.states.async_set(DOOR, "off")
    window = await started(hass, window_context(minutes=10))
    hass.states.async_set(DOOR, "on")
    await move(hass, freezer, "22:20:00")
    hass.states.async_set(DOOR, "off")
    await hass.async_block_till_done()
    assert lines(window) == [(DOOR, "on", "off")]
    assert window.buffered == 2
    window.async_stop()


@pytest.mark.parametrize(
    ("collapse", "expected"),
    [
        (True, [(DOOR, "off", "on")]),
        (False, [(DOOR, "off", "off"), (DOOR, "off", "on"), (DOOR, "on", "on")]),
    ],
)
async def test_repeats_are_collapsed_unless_asked_for(
    hass: HomeAssistant,
    collapse: bool,
    expected: list[tuple[str, str, str]],
) -> None:
    hass.states.async_set(DOOR, "off")
    window = await started(hass, window_context(collapse_repeats=collapse))
    # Attribute-only updates repeat the state already logged.
    hass.states.async_set(DOOR, "off", {"battery": 90})
    hass.states.async_set(DOOR, "on", {"battery": 90})
    hass.states.async_set(DOOR, "on", {"battery": 80})
    await hass.async_block_till_done()
    assert lines(window) == expected
    window.async_stop()


async def test_a_removed_entity_logs_nothing(hass: HomeAssistant) -> None:
    hass.states.async_set(DOOR, "off")
    window = await started(hass, window_context())
    hass.states.async_remove(DOOR)
    await hass.async_block_till_done()
    assert lines(window) == []
    window.async_stop()


async def test_attributes_are_logged_only_when_asked_for_and_never_the_private_ones(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set(DOOR, "off")
    plain = await started(hass, window_context())
    detailed = await started(hass, window_context(with_attributes=True))
    hass.states.async_set(
        DOOR,
        "on",
        {
            "friendly_name": "Front door",
            "device_class": "door",
            "battery": 90,
            "api_key": "k",
            "Access_Token_Expiry": "soon",
            "entity_picture_local": "/local/door.png",
            "media_image_url": "/api/x",
            "image_urls": ["/api/y"],
            "sensors": ({"name": "reed", "config_url": "/x", "password": "p"},),
        },
    )
    await hass.async_block_till_done()
    now = dt_util.utcnow()
    assert plain.entries(now)[0].attributes is None
    assert detailed.entries(now)[0].attributes == {
        "battery": 90,
        "sensors": [{"name": "reed"}],
    }
    plain.async_stop()
    detailed.async_stop()


def test_an_event_filter_is_an_exact_subset() -> None:
    assert matches(CAMERA, {"camera": "front", "score": 0.9})
    assert not matches(CAMERA, {"camera": "back"})
    assert not matches(CAMERA, {})
    assert matches(EventFilter(event_type="any"), {"whatever": 1})


async def test_matching_bus_events_are_logged_without_their_secrets(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set(DOOR, "off")
    window = await started(
        hass,
        window_context(events=(CAMERA, EventFilter(event_type="doorbell"))),
    )
    hass.bus.async_fire("person_detected", {"camera": "back"})
    hass.bus.async_fire(
        "person_detected",
        {
            "camera": "front",
            "access_token": "t",
            "snapshot_url": "u",
            # Nested data is redacted at every depth.
            "detection": {
                "score": 0.9,
                "clip": {"URL": "u", "stream_uri": "rtsp", "length": 4},
                "frames": [{"index": 1, "token": "t", "image_url": "u"}],
            },
            "uri": "u",
        },
    )
    hass.bus.async_fire("doorbell", {})
    hass.bus.async_fire("something_else", {"camera": "front"})
    await hass.async_block_till_done()
    camera, bell = window.entries(dt_util.utcnow())
    assert camera.kind == "event"
    assert camera.event_type == "person_detected"
    assert camera.name == "front door camera saw a person"
    assert camera.attributes == {
        "camera": "front",
        "detection": {"score": 0.9, "clip": {"length": 4}, "frames": [{"index": 1}]},
    }
    assert (bell.event_type, bell.name, bell.attributes) == ("doorbell", "doorbell", None)
    window.async_stop()


async def test_a_state_changed_event_is_logged_as_plain_data(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set(DOOR, "off")
    window = await started(
        hass,
        window_context(
            events=(EventFilter(event_type="state_changed", data={"entity_id": PHONE}),)
        ),
    )
    hass.states.async_set(
        PHONE,
        "on",
        {
            "friendly_name": "Phone charging",
            "entity_picture": "/api/image/secret",
            "since": at("21:00:00"),
            "chargers": {"wall"},
        },
    )
    await hass.async_block_till_done()
    [entry] = window.entries(dt_util.utcnow())
    data = entry.attributes
    assert data is not None
    # The State objects arrive as plain data the request can carry, and the
    # redaction reaches inside them.
    json.dumps(data)
    assert data["old_state"] is None
    new = data["new_state"]
    assert new["state"] == "on"
    assert new["attributes"] == {
        "friendly_name": "Phone charging",
        "since": "2026-10-30T21:00:00+00:00",
        "chargers": ["wall"],
    }
    assert isinstance(new["last_changed"], str)
    window.async_stop()


async def test_event_data_nobody_can_encode_goes_as_text(hass: HomeAssistant) -> None:
    class Opaque:
        def __str__(self) -> str:
            return "opaque thing"

    hass.states.async_set(DOOR, "off")
    window = await started(hass, window_context(events=(EventFilter("custom"),)))
    hass.bus.async_fire("custom", {"what": Opaque(), "when": at("21:00:00")})
    await hass.async_block_till_done()
    [entry] = window.entries(dt_util.utcnow())
    assert entry.attributes == {
        "what": "opaque thing",
        "when": "2026-10-30T21:00:00+00:00",
    }
    window.async_stop()


async def test_wake_on_feed_the_log_instead_of_the_target(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set(DOOR, "off")
    hass.states.async_set(MOTION, "off")
    window = await started(hass, window_context((MOTION,), wake_on=(DOOR,)))
    assert window.entity_ids == [DOOR]
    hass.states.async_set(MOTION, "on")
    hass.states.async_set(DOOR, "on")
    await hass.async_block_till_done()
    assert lines(window) == [(DOOR, "off", "on")]
    window.async_stop()


async def test_the_entity_cap_keeps_the_first_ids(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    for entity_id in (DOOR, MOTION, PHONE):
        hass.states.async_set(entity_id, "off")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("custom_components.mentat.window.MAX_WINDOW_ENTITIES", 2)
        window = await started(hass, window_context((DOOR, MOTION, PHONE)))
        assert window.entity_ids == [PHONE, DOOR]
        hass.states.async_set(MOTION, "on")
        hass.states.async_set(DOOR, "on")
        await hass.async_block_till_done()
    assert lines(window) == [(DOOR, "off", "on")]
    assert "targets 3 entities; its window logs the first 2" in caplog.text
    window.async_stop()


async def test_a_watch_without_a_window_has_no_log(hass: HomeAssistant) -> None:
    watch = WatchConfig(key="plain", name="Plain", questions=(), scan_interval=300)
    with pytest.raises(ValueError, match="has no window"):
        EventWindow(hass, watch)


async def test_a_window_with_nothing_to_follow_stays_empty(hass: HomeAssistant) -> None:
    watch = replace(window_context(), target=None)
    window = await started(hass, watch)
    assert window.entity_ids == []
    window.async_stop()


async def test_stop_unsubscribes_everything(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    hass.states.async_set(DOOR, "off")
    fired: list[datetime] = []

    @callback
    def settled() -> None:
        fired.append(dt_util.utcnow())

    window = await started(hass, window_context(events=(CAMERA,), settle=5), settled)
    hass.states.async_set(DOOR, "on")
    await hass.async_block_till_done()
    window.async_stop()
    hass.states.async_set(DOOR, "off")
    hass.bus.async_fire("person_detected", {"camera": "front"})
    freezer.tick(60)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert lines(window) == [(DOOR, "off", "on")]
    # The run the first change scheduled is cancelled with the rest.
    assert fired == []


# --- settling ---


async def _settling(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, settle: float
) -> tuple[EventWindow, list[datetime]]:
    await move(hass, freezer, "22:00:00")
    hass.states.async_set(DOOR, "off")
    hass.states.async_set(MOTION, "off")
    fired: list[datetime] = []

    @callback
    def settled() -> None:
        fired.append(dt_util.utcnow())

    return await started(hass, window_context(settle=settle), settled), fired


async def test_the_window_is_judged_after_the_quiet(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    window, fired = await _settling(hass, freezer, 10)
    hass.states.async_set(DOOR, "on")
    await move(hass, freezer, "22:00:05")
    hass.states.async_set(MOTION, "on")
    # Ten seconds after the first change is not ten seconds of quiet.
    await move(hass, freezer, "22:00:14")
    assert fired == []
    await move(hass, freezer, "22:00:15")
    assert fired == [at("22:00:15")]
    hass.states.async_set(DOOR, "off")
    await move(hass, freezer, "22:00:24")
    assert len(fired) == 1
    await move(hass, freezer, "22:00:25")
    assert len(fired) == 2
    window.async_stop()


async def test_a_change_every_few_seconds_cannot_put_the_judging_off(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    window, fired = await _settling(hass, freezer, 10)
    # A change at 0, 8, 16 and 24 seconds: never ten seconds of quiet.
    hass.states.async_set(DOOR, "on")
    for clock, state in (("22:00:08", "off"), ("22:00:16", "on"), ("22:00:24", "off")):
        await move(hass, freezer, clock)
        hass.states.async_set(DOOR, state)
    await move(hass, freezer, "22:00:29")
    assert fired == []
    # Three settle periods after the first change still waiting.
    await move(hass, freezer, "22:00:30")
    assert fired == [at("22:00:30")]
    # The cap starts again with the next change.
    hass.states.async_set(DOOR, "on")
    await move(hass, freezer, "22:00:39")
    assert len(fired) == 1
    await move(hass, freezer, "22:00:40")
    assert len(fired) == 2
    window.async_stop()


async def test_a_settle_of_zero_judges_every_change(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    window, fired = await _settling(hass, freezer, 0)
    hass.states.async_set(DOOR, "on")
    hass.states.async_set(MOTION, "on")
    await hass.async_block_till_done()
    assert fired == [at("22:00:00")] * 2
    window.async_stop()


async def test_without_on_change_the_log_only_fills(hass: HomeAssistant) -> None:
    hass.states.async_set(DOOR, "off")
    window = await started(hass, window_context(settle=0))
    hass.states.async_set(DOOR, "on")
    await hass.async_block_till_done()
    assert window.buffered == 1
    window.async_stop()


# --- the recorder ---


async def test_the_recorder_fills_the_window_at_start(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, mock_recorder: FakeRecorder
) -> None:
    await move(hass, freezer, "22:41:00")
    hass.states.async_set(DOOR, "on", {"friendly_name": "Front door"})
    hass.states.async_set(MOTION, "off")
    mock_recorder.history = {
        # The state in force when the window opened is the first change's from.
        MOTION: [
            State(MOTION, "off", last_changed=at("21:00:00")),
            State(MOTION, "on", last_changed=at("22:15:00")),
            State(MOTION, "off", last_changed=at("22:16:00")),
        ],
        # No state before the window: the first change comes from nothing.
        DOOR: [State(DOOR, "on", last_changed=at("22:12:00"))],
    }
    fired: list[datetime] = []
    window = await started(hass, window_context(), lambda: fired.append(at("00:00")))
    assert lines(window) == [
        (DOOR, None, "on"),
        (MOTION, "off", "on"),
        (MOTION, "on", "off"),
    ]
    assert window.entries(at("22:41:00"))[0].name == "Front door"
    # One query reads both entities.
    [query] = mock_recorder.calls
    assert query["entity_ids"] == [DOOR, MOTION]
    assert query["start"] == at("22:11:00")
    assert query["end"] == at("22:41:00")
    assert query["no_attributes"] is True
    assert query["significant_changes_only"] is True
    assert query["include_start_time_state"] is True
    # What happened before the window opened is no reason to judge it again.
    await hass.async_block_till_done()
    assert fired == []
    window.async_stop()


async def test_the_backfill_keeps_the_newest_and_attributes_when_asked(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, mock_recorder: FakeRecorder
) -> None:
    await move(hass, freezer, "22:41:00")
    mock_recorder.history = {
        DOOR: [
            State(
                DOOR,
                "on" if minute % 2 else "off",
                {"battery": minute, "access_token": "t"},
                last_changed=at(f"22:{minute:02d}:00"),
            )
            for minute in range(12, 24)
        ]
    }
    window = await started(hass, window_context(max_events=10, with_attributes=True), live=False)
    now = dt_util.utcnow()
    entries = window.entries(now)
    assert len(entries) == 10
    assert entries[0].at == at("22:14:00")
    assert entries[0].attributes == {"battery": 14}
    # The name falls back to the id for an entity with no state now.
    assert entries[0].name == DOOR
    assert window.truncated(now)
    assert mock_recorder.calls[0]["no_attributes"] is False


async def test_a_change_while_the_recorder_is_read_comes_after_the_backfill(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, mock_recorder: FakeRecorder
) -> None:
    await move(hass, freezer, "22:41:00")
    hass.states.async_set(DOOR, "off")
    mock_recorder.history = {
        DOOR: [
            State(DOOR, "off", last_changed=at("22:00:00")),
            State(DOOR, "on", last_changed=at("22:20:00")),
            State(DOOR, "off", last_changed=at("22:21:00")),
        ]
    }
    mock_recorder.during = lambda: hass.states.async_set(DOOR, "on")
    window = await started(hass, window_context((DOOR,)))
    await hass.async_block_till_done()
    assert lines(window) == [
        (DOOR, "off", "on"),
        (DOOR, "on", "off"),
        (DOOR, "off", "on"),
    ]
    window.async_stop()


async def test_a_recorder_that_fails_leaves_the_window_empty(
    hass: HomeAssistant, mock_recorder: FakeRecorder, caplog: pytest.LogCaptureFixture
) -> None:
    hass.states.async_set(DOOR, "off")
    mock_recorder.error = OperationalError("SELECT", {}, Exception("locked"))
    with caplog.at_level(logging.WARNING):
        window = await started(hass, window_context(), live=False)
    assert window.buffered == 0
    assert "the recorder could not be read" in caplog.text


async def test_a_backfill_that_raises_still_lets_changes_in(
    hass: HomeAssistant, mock_recorder: FakeRecorder
) -> None:
    hass.states.async_set(DOOR, "off")
    mock_recorder.error = RuntimeError("disk gone")
    window = EventWindow(hass, window_context((DOOR,)))
    with pytest.raises(RuntimeError):
        await window.async_start()
    hass.states.async_set(DOOR, "on")
    await hass.async_block_till_done()
    assert lines(window) == [(DOOR, "off", "on")]
    window.async_stop()


async def test_the_backfill_drops_a_climate_row_that_kept_its_state(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, mock_recorder: FakeRecorder
) -> None:
    """The recorder returns a thermostat's attribute updates among its changes."""
    await move(hass, freezer, "22:41:00")
    mock_recorder.history = {
        THERMOSTAT: [
            State(THERMOSTAT, "heat", last_changed=at("22:00:00")),
            State(THERMOSTAT, "heat", last_changed=at("22:20:00")),
            State(THERMOSTAT, "off", last_changed=at("22:30:00")),
        ],
        DOOR: [
            State(DOOR, "off", last_changed=at("22:20:00")),
            State(DOOR, "off", last_changed=at("22:25:00")),
        ],
    }
    watch = window_context((DOOR, THERMOSTAT), collapse_repeats=False)
    window = await started(hass, watch, live=False)
    # A door's repeated state is a row the recorder kept for a reason, so it stays.
    assert lines(window) == [(DOOR, None, "off"), (DOOR, "off", "off"), (THERMOSTAT, "heat", "off")]


async def test_a_window_of_mentat_answers_alone_reads_no_recorder(
    hass: HomeAssistant, mock_recorder: FakeRecorder
) -> None:
    answer = er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, "elsewhere_answer", suggested_object_id="neighbour_answer"
    )
    hass.states.async_set(answer.entity_id, "0.45")
    window = await started(hass, window_context((answer.entity_id,)), live=False)
    assert window.buffered == 0
    assert mock_recorder.calls == []


async def test_no_recorder_no_backfill(hass: HomeAssistant) -> None:
    hass.states.async_set(DOOR, "off")
    assert "recorder" not in hass.config.components
    window = await started(hass, window_context(), live=False)
    assert window.buffered == 0


# --- the state the model reads ---


def test_span_reads_like_speech_from_seconds_to_days() -> None:
    assert span(timedelta(seconds=-3)) == "0 s"
    assert span(timedelta(seconds=42)) == "42 s"
    assert span(timedelta(minutes=19, seconds=20)) == "19 min"
    assert span(timedelta(hours=3, minutes=12)) == "3.2 h"
    assert span(timedelta(days=3)) == "3.0 d"
    assert ago(timedelta(minutes=2)) == "2 min ago"


async def test_window_state_carries_the_log_and_the_arithmetic(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await move(hass, freezer, "19:29:00")
    hass.states.async_set(ALICE, "home", {"friendly_name": "Alice"})
    hass.states.async_set(PHONE, "off", {"friendly_name": "Alice phone charging"})
    motion = {"friendly_name": "Living room motion", "device_class": "motion"}
    hass.states.async_set(MOTION, "off", motion)
    await move(hass, freezer, "22:05:00")
    watch = window_context((ALICE, PHONE, MOTION), events=(CAMERA,))
    window = await started(hass, watch)
    await move(hass, freezer, "22:17:00")
    hass.states.async_set(PHONE, "on", {"friendly_name": "Alice phone charging"})
    for clock, value in (("22:19", "on"), ("22:20", "off"), ("22:21", "on")):
        await move(hass, freezer, f"{clock}:00")
        hass.states.async_set(MOTION, value, motion)
    await move(hass, freezer, "22:22:00")
    hass.states.async_set(MOTION, "off", motion)
    await move(hass, freezer, "22:30:00")
    hass.bus.async_fire("person_detected", {"camera": "front", "access_token": "t"})
    await move(hass, freezer, "22:41:00")

    assert watch.window is not None
    state = async_build_window_state(
        hass, "Alice works nights.", {"entity_id": [ALICE, PHONE, MOTION]}, window, dt_util.utcnow()
    )
    assert state == {
        "now": "2026-10-30 22:41 Friday",
        "window": {
            "from": "22:11",
            "to": "22:41",
            "minutes": 30,
            "events": 6,
            "truncated": False,
        },
        "entities": [
            {
                "entity_id": PHONE,
                "name": "Alice phone charging",
                "state": "on",
                "since": "24 min ago",
                "changes_in_window": 1,
                "last_on": "now",
                "on_total": "24 min",
            },
            {
                "entity_id": MOTION,
                "name": "Living room motion",
                "device_class": "motion",
                "state": "off",
                "since": "19 min ago",
                "changes_in_window": 4,
                "last_on": "19 min ago",
                "on_total": "2 min",
            },
            {
                "entity_id": ALICE,
                "name": "Alice",
                "state": "home",
                "since": "3.2 h ago",
                "changes_in_window": 0,
            },
        ],
        "events": [
            {
                "at": "22:17",
                "ago": "24 min ago",
                "entity_id": PHONE,
                "name": "Alice phone charging",
                "from": "off",
                "to": "on",
            },
            {
                "at": "22:19",
                "ago": "22 min ago",
                "entity_id": MOTION,
                "name": "Living room motion",
                "from": "off",
                "to": "on",
            },
            {
                "at": "22:20",
                "ago": "21 min ago",
                "entity_id": MOTION,
                "name": "Living room motion",
                "from": "on",
                "to": "off",
            },
            {
                "at": "22:21",
                "ago": "20 min ago",
                "entity_id": MOTION,
                "name": "Living room motion",
                "from": "off",
                "to": "on",
            },
            {
                "at": "22:22",
                "ago": "19 min ago",
                "entity_id": MOTION,
                "name": "Living room motion",
                "from": "on",
                "to": "off",
            },
            {
                "at": "22:30",
                "ago": "11 min ago",
                "event": "person_detected",
                "name": "front door camera saw a person",
                "data": {"camera": "front"},
            },
        ],
        "note": "Alice works nights.",
    }
    window.async_stop()


async def test_window_state_counts_on_time_before_the_first_change_and_without_one(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await move(hass, freezer, "21:00:00")
    hass.states.async_set(DOOR, "on")
    hass.states.async_set(MOTION, "off")
    await move(hass, freezer, "22:20:00")
    hass.states.async_set(PHONE, "on")
    watch = window_context((DOOR, MOTION, PHONE))
    window = await started(hass, watch)
    await move(hass, freezer, "22:30:00")
    hass.states.async_set(DOOR, "off")
    await move(hass, freezer, "22:41:00")
    assert watch.window is not None
    phone, door, motion = async_build_window_state(
        hass, None, {"entity_id": [DOOR, MOTION, PHONE]}, window, dt_util.utcnow()
    )["entities"]
    # Open from before the window until 22:30: 19 minutes of it.
    assert (door["on_total"], door["last_on"]) == ("19 min", "11 min ago")
    # Never on in the window.
    assert motion["on_total"] == "0 s"
    assert "last_on" not in motion
    # On since 22:20 with no change logged: only the known part counts.
    assert (phone["on_total"], phone["last_on"]) == ("21 min", "now")
    window.async_stop()


async def test_window_state_names_the_weekday_of_a_boundary_on_another_day(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await move(hass, freezer, "01:00:00")
    hass.states.async_set(DOOR, "off")
    watch = window_context((DOOR,), minutes=180)
    window = await started(hass, watch)
    hass.states.async_set(DOOR, "on", {"friendly_name": DOOR})
    await move(hass, freezer, "02:00:00")
    assert watch.window is not None
    state = async_build_window_state(hass, None, {"entity_id": [DOOR]}, window, dt_util.utcnow())
    assert state["window"]["from"] == "Thu 23:00"
    assert state["events"] == [
        {"at": "01:00", "ago": "1.0 h ago", "entity_id": DOOR, "from": "off", "to": "on"}
    ]
    assert "note" not in state
    window.async_stop()


async def test_window_state_carries_asked_for_attributes_without_private_keys(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set(DOOR, "off")
    watch = window_context((DOOR,), with_attributes=True)
    window = await started(hass, watch)
    hass.states.async_set(DOOR, "on", {"battery": 90, "entity_picture": "/x"})
    await hass.async_block_till_done()
    assert watch.window is not None
    state = async_build_window_state(
        hass, None, {"entity_id": [DOOR]}, window, dt_util.utcnow(), with_attributes=True
    )
    [row] = state["events"]
    assert row["attributes"] == {"battery": 90}
    assert state["entities"][0]["attributes"] == {"battery": 90}
    window.async_stop()


async def test_window_state_refuses_more_entities_than_a_window_takes(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set(DOOR, "off")
    hass.states.async_set(MOTION, "off")
    watch = window_context()
    window = await started(hass, watch, live=False)
    assert watch.window is not None
    with (
        pytest.MonkeyPatch.context() as patch,
        pytest.raises(ServiceValidationError) as err,
    ):
        patch.setattr("custom_components.mentat.window.MAX_WINDOW_ENTITIES", 1)
        async_build_window_state(
            hass, None, {"entity_id": [DOOR, MOTION]}, window, dt_util.utcnow()
        )
    assert err.value.translation_key == "target_too_large"
    assert err.value.translation_placeholders == {"count": "2", "limit": "1"}


def test_an_entry_is_small() -> None:
    entry = WindowEntry(
        at=at("22:00:00"),
        kind="state",
        entity_id=DOOR,
        name="Front door",
        old="off",
        new="on",
    )
    assert entry.attributes is None
    assert entry.event_type is None
