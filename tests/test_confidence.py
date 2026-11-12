"""A floor under a choice or a score: what the sensor and the event show below it."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from jevclient import ChoiceAnswer, ScoreAnswer
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.mentat.const import EVENT_DECISION

from .conftest import CHOICE_KEY, SCORE_KEY, WATCH, LoadYaml, build_response, laundry_answers

CHOICE = "sensor.typesafe_programme"
SCORE = "sensor.typesafe_urgency"


def with_rules(**rules: Any) -> list[dict[str, Any]]:
    """WATCH with these settings on its choice and, without a fallback, its score."""
    noul, choice, score = WATCH["questions"]
    plain = {key: value for key, value in rules.items() if key != "fallback"}
    if isinstance(plain.get("min_confidence"), dict):
        plain = {}
    return [{**WATCH, "questions": [noul, {**choice, **rules}, {**score, **plain}]}]


def spin(confidence: float, choice: str = "spin") -> ChoiceAnswer:
    probabilities = {"wash": 0.1, "spin": 0.85, "idle": 0.05}
    return ChoiceAnswer(choice=choice, probabilities=probabilities, confidence=confidence)


async def test_choice_under_its_floor_shows_the_fallback_and_keeps_the_model_word_beside_it(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    fake_client.ask.return_value = build_response(**laundry_answers())
    events = async_capture_events(hass, EVENT_DECISION)
    await yaml_watch(with_rules(min_confidence=0.9, fallback="unsure"))
    sensor = hass.states.get(CHOICE)
    assert sensor.state == "unsure"
    assert sensor.attributes["options"] == ["wash", "spin", "idle", "unsure"]
    assert sensor.attributes["confident"] is False
    assert sensor.attributes["leaning"] == "spin"
    assert sensor.attributes["confidence"] == 0.8
    shown = events[-1].data["answers"][CHOICE_KEY]
    assert (shown["choice"], shown["confident"]) == ("spin", False)


async def test_fallback_that_is_an_option_is_not_listed_twice(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    fake_client.ask.return_value = build_response(**laundry_answers())
    await yaml_watch(with_rules(min_confidence=0.9, fallback="idle"))
    sensor = hass.states.get(CHOICE)
    assert sensor.state == "idle"
    assert sensor.attributes["options"] == ["wash", "spin", "idle"]


async def test_fallback_never_reaches_the_model(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    fake_client.ask.return_value = build_response(**laundry_answers())
    await yaml_watch(with_rules(min_confidence=0.9, fallback="unsure"))
    assert list(fake_client.ask.await_args.args[1][CHOICE_KEY].criteria) == ["wash", "spin", "idle"]


@pytest.mark.parametrize(
    ("answer", "state"),
    [(spin(0.8), "idle"), (spin(0.8, "wash"), "wash"), (spin(0.3, "wash"), "idle")],
    ids=["own-bar", "default-bar", "under-default"],
)
async def test_choice_option_with_a_bar_of_its_own(
    hass: HomeAssistant,
    fake_client: MagicMock,
    yaml_watch: LoadYaml,
    answer: ChoiceAnswer,
    state: str,
) -> None:
    fake_client.ask.return_value = build_response(**{CHOICE_KEY: answer})
    await yaml_watch(with_rules(min_confidence={"default": 0.5, "spin": 0.85}, fallback="idle"))
    assert hass.states.get(CHOICE).state == state


async def test_choice_at_its_bar_is_confident(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    fake_client.ask.return_value = build_response(**laundry_answers())
    await yaml_watch(with_rules(min_confidence=0.8))
    sensor = hass.states.get(CHOICE)
    assert (sensor.state, sensor.attributes["confident"]) == ("spin", True)
    assert "leaning" not in sensor.attributes


async def test_mapping_with_no_bar_for_the_winner_asks_nothing_of_it(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    fake_client.ask.return_value = build_response(**laundry_answers())
    await yaml_watch(with_rules(min_confidence={"wash": 0.99}))
    sensor = hass.states.get(CHOICE)
    assert sensor.state == "spin"
    assert "confident" not in sensor.attributes


@pytest.mark.parametrize(("entity_id", "leaning"), [(CHOICE, "spin"), (SCORE, "Today")])
async def test_under_its_floor_with_no_fallback_reads_unknown_and_stays_available(
    hass: HomeAssistant,
    fake_client: MagicMock,
    yaml_watch: LoadYaml,
    entity_id: str,
    leaning: str,
) -> None:
    fake_client.ask.return_value = build_response(**laundry_answers())
    await yaml_watch(with_rules(min_confidence=0.95))
    sensor = hass.states.get(entity_id)
    assert sensor.state == "unknown"
    assert sensor.attributes["confident"] is False
    assert sensor.attributes["leaning"] == leaning


async def test_no_floor_changes_nothing(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    fake_client.ask.return_value = build_response(**laundry_answers())
    events = async_capture_events(hass, EVENT_DECISION)
    await yaml_watch()
    for entity_id in (CHOICE, SCORE):
        assert "confident" not in hass.states.get(entity_id).attributes
    assert "confident" not in events[-1].data["answers"][SCORE_KEY]


async def test_score_event_says_it_fell_short_and_keeps_its_score(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    answer = laundry_answers()[SCORE_KEY]
    assert isinstance(answer, ScoreAnswer)
    fake_client.ask.return_value = build_response(**{SCORE_KEY: answer})
    events = async_capture_events(hass, EVENT_DECISION)
    await yaml_watch(with_rules(min_confidence=0.95))
    shown = events[-1].data["answers"][SCORE_KEY]
    assert (shown["score"], shown["confident"]) == (answer.score, False)
