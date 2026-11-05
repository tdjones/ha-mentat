"""mentat.calibrate: a cutoff and a release from the recorder, at no cost."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import Context, HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser
from sqlalchemy.exc import OperationalError

from custom_components.mentat.calibrate import Piece, pieces_of, replay
from custom_components.mentat.const import DOMAIN

from .conftest import FakeRecorder

NOUL = "sensor.mentat_load_waiting"
TRUTH = "binary_sensor.washing_machine_running"
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=dt_util.UTC)
START = NOW - timedelta(days=1)


def hours(after: float) -> datetime:
    return START + timedelta(hours=after)


def states(entity_id: str, *changes: tuple[float, str]) -> list[State]:
    """States changing at these hours after the start; a negative hour is before it."""
    return [State(entity_id, value, last_changed=hours(at)) for at, value in changes]


# Two washes the noul caught, at 6 h and 18 h, and one false alarm at 12 h.
PROBABILITIES = states(
    NOUL, (-1, "0.1"), (6, "0.9"), (8, "0.2"), (12, "0.75"), (13, "0.1"), (18, "0.95"), (20, "0.05")
)
TRUTHS = states(TRUTH, (-1, "off"), (6, "on"), (8, "off"), (18, "on"), (20, "off"))


async def calibrate(hass: HomeAssistant, user_id: str | None = None, **data: Any) -> dict[str, Any]:
    response: dict[str, Any] = await hass.services.async_call(
        DOMAIN,
        "calibrate",
        {"sensor": NOUL, "truth": TRUTH, "days": 1, **data},
        blocking=True,
        return_response=True,
        context=Context(user_id=user_id),
    )
    return response


@pytest.fixture
def recorded(
    loaded: MockConfigEntry, mock_recorder: FakeRecorder, freezer: FrozenDateTimeFactory
) -> FakeRecorder:
    freezer.move_to(NOW)
    mock_recorder.history = {NOUL: PROBABILITIES, TRUTH: TRUTHS}
    return mock_recorder


async def test_calibrate_replays_the_verdict_and_sends_nothing(
    hass: HomeAssistant, recorded: FakeRecorder, fake_client: MagicMock
) -> None:
    asked_before = fake_client.ask.await_count
    fit = await calibrate(hass)
    # Cutoffs of 0.8 to 0.9 catch both washes and skip the alarm, and releases of
    # 0.25 up to the cutoff turn off again. The middle of each is taken.
    assert (fit["cutoff"], fit["release"]) == (0.85, 0.55)
    assert (fit["precision"], fit["recall"], fit["f1"]) == (1.0, 1.0, 1.0)
    assert (fit["caught"], fit["missed"], fit["false_alarms"], fit["flips"]) == (2, 0, 0, 4)
    assert fit["when_true"] == {"low": 0.9, "high": 0.95}
    assert fit["when_false"] == {"low": 0.05, "high": 0.2}
    assert fit["overlap"] is False
    assert (fit["hours"], fit["hours_true"], fit["answers"]) == (24.0, 4.0, 7)
    assert fit["without_release"]["flips"] == 4
    [query] = recorded.calls
    assert query["entity_ids"] == [NOUL, TRUTH]
    assert query["start"] == START
    assert fake_client.ask.await_count == asked_before


async def test_calibrate_says_when_no_numbers_can_separate_true_from_false(
    hass: HomeAssistant, recorded: FakeRecorder
) -> None:
    recorded.history[NOUL] = states(NOUL, (-1, "0.5"), (6, "0.45"), (8, "0.55"), (18, "0.5"))
    fit = await calibrate(hass)
    assert fit["overlap"] is True


async def test_calibrate_reads_the_true_state_it_is_given(
    hass: HomeAssistant, recorded: FakeRecorder
) -> None:
    recorded.history[TRUTH] = states(TRUTH, (-1, "idle"), (6, "running"), (8, "idle"))
    fit = await calibrate(hass, truth_state="running")
    assert (fit["hours_true"], fit["caught"] + fit["missed"]) == (2.0, 1)


@pytest.mark.parametrize(
    ("history", "key"),
    [
        ({NOUL: [], TRUTH: TRUTHS}, "calibrate_no_history"),
        ({NOUL: states(NOUL, (-1, "unavailable")), TRUTH: TRUTHS}, "calibrate_no_history"),
        ({NOUL: PROBABILITIES, TRUTH: states(TRUTH, (-1, "off"))}, "calibrate_never_true"),
        ({NOUL: PROBABILITIES, TRUTH: states(TRUTH, (-1, "on"))}, "calibrate_always_true"),
        ({NOUL: states(NOUL, (-1, "2.4")), TRUTH: TRUTHS}, "calibrate_not_probability"),
    ],
    ids=["nothing", "never-a-number", "never-true", "always-true", "a-score"],
)
async def test_calibrate_refuses_a_history_with_nothing_to_separate(
    hass: HomeAssistant, recorded: FakeRecorder, history: dict[str, list[State]], key: str
) -> None:
    recorded.history = history
    with pytest.raises(ServiceValidationError) as caught:
        await calibrate(hass)
    assert caught.value.translation_key == key
    assert caught.value.translation_placeholders == {
        "sensor": NOUL,
        "truth": TRUTH,
        "state": "on",
        "days": "1",
    }


async def test_calibrate_without_the_recorder_says_so(
    hass: HomeAssistant, recorded: FakeRecorder
) -> None:
    hass.config.components.remove("recorder")
    with pytest.raises(ServiceValidationError) as caught:
        await calibrate(hass)
    assert caught.value.translation_key == "calibrate_needs_recorder"
    assert recorded.calls == []


async def test_calibrate_with_an_unreadable_recorder_says_why(
    hass: HomeAssistant, recorded: FakeRecorder
) -> None:
    recorded.error = OperationalError("SELECT", {}, Exception("database is locked"))
    with pytest.raises(HomeAssistantError) as caught:
        await calibrate(hass)
    assert caught.value.translation_key == "calibrate_recorder_failed"
    assert "database is locked" in caught.value.translation_placeholders["reason"]


async def test_calibrate_refuses_a_user_who_may_not_read_the_truth_before_reading_history(
    hass: HomeAssistant, recorded: FakeRecorder
) -> None:
    user = MockUser().add_to_hass(hass)
    user.mock_policy({"entities": {"entity_ids": {NOUL: True}}})
    with pytest.raises(ServiceValidationError) as caught:
        await calibrate(hass, user.id)
    assert caught.value.translation_key == "entity_not_permitted"
    assert caught.value.translation_placeholders == {"entity_id": TRUTH}
    assert recorded.calls == []


# --- the pieces on their own ---


def test_pieces_count_from_the_start_and_leave_out_a_sensor_with_no_number() -> None:
    pieces = pieces_of(
        states(NOUL, (-3, "0.4"), (2, "unavailable"), (3, "0.6")),
        states(TRUTH, (-5, "off"), (1, "on")),
        "on",
        START,
        hours(4),
    )
    assert pieces == [
        Piece(0.4, False, 3600.0),
        Piece(0.4, True, 3600.0),
        Piece(0.6, True, 3600.0),
    ]


def wobble(*probabilities: float, truth: bool = False) -> list[Piece]:
    return [Piece(probability, truth, 60.0) for probability in probabilities]


def test_replay_holds_a_wobble_that_a_lone_cutoff_flips_on() -> None:
    pieces = wobble(0.1, 0.72, 0.68, 0.72, 0.68, 0.72, 0.1)
    alone = replay(pieces, 0.7, 0.7)
    held = replay(pieces, 0.7, 0.3)
    assert (alone.flips, alone.false_alarms) == (6, 3)
    assert (held.flips, held.false_alarms) == (2, 1)


def test_replay_counts_a_stretch_it_never_turned_on_for_as_missed() -> None:
    pieces = [*wobble(0.1), *wobble(0.2, truth=True), *wobble(0.1), *wobble(0.9, truth=True)]
    run = replay(pieces, 0.7, 0.3)
    assert (run.caught, run.missed, run.false_alarms) == (1, 1, 0)
    assert run.recall == 0.5


def test_replay_of_an_alarm_still_on_at_the_end_is_counted() -> None:
    assert replay(wobble(0.9), 0.7, 0.3).false_alarms == 1


def test_replay_of_a_miss_still_running_at_the_end_is_counted() -> None:
    assert replay(wobble(0.1, truth=True), 0.7, 0.3).missed == 1


def test_replay_counts_true_time_under_an_unknown_verdict_as_missed() -> None:
    run = replay(wobble(0.5, truth=True), 0.7, 0.3)
    assert (run.precision, run.recall, run.f1, run.missed) == (None, 0.0, 0.0, 1)


def test_replay_of_no_true_time_has_no_recall() -> None:
    assert replay(wobble(0.1), 0.7, 0.3).recall is None
