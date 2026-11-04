"""A noul's verdict against its cutoff and release, and the budget tripwire."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from jevclient import NoulAnswer
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.mentat.const import EVENT_DECISION

from .conftest import NOUL_KEY, PROBE_TOKENS, WATCH, LoadYaml, build_response

VERDICT = "binary_sensor.typesafe_laundry_forgotten"


def forgotten_only(**question: Any) -> list[dict[str, Any]]:
    """WATCH with just its noul, changed as given."""
    noul = {**WATCH["questions"][0], **question}
    return [{**WATCH, "questions": [noul]}]


@pytest.mark.parametrize(
    ("noul", "cutoff", "state"),
    [(0.9, 0.6, "on"), (0.6, 0.6, "on"), (0.59, 0.6, "off"), (0.0, 0.0, "on")],
)
async def test_verdict_is_on_at_or_over_the_cutoff(
    hass: HomeAssistant,
    fake_client: MagicMock,
    yaml_watch: LoadYaml,
    noul: float,
    cutoff: float,
    state: str,
) -> None:
    fake_client.ask.return_value = build_response(**{NOUL_KEY: NoulAnswer(noul=noul)})
    await yaml_watch(forgotten_only(cutoff=cutoff))
    verdict = hass.states.get(VERDICT)
    assert verdict.state == state
    assert verdict.attributes["probability"] == noul
    assert verdict.attributes["cutoff"] == cutoff


async def test_verdict_absent_without_a_cutoff(hass: HomeAssistant, yaml_watch: LoadYaml) -> None:
    question = {key: value for key, value in WATCH["questions"][0].items() if key != "cutoff"}
    await yaml_watch([{**WATCH, "questions": [question]}])
    assert hass.states.get(VERDICT) is None
    assert hass.states.get("sensor.typesafe_laundry_forgotten") is not None


async def test_over_budget_sensor_reads_the_ledger(
    hass: HomeAssistant, yaml_watch: LoadYaml
) -> None:
    await yaml_watch([])
    tripwire = hass.states.get("binary_sensor.typesafe_over_budget")
    assert tripwire.state == "off"
    assert tripwire.attributes["device_class"] == "problem"
    assert tripwire.attributes["token_budget"] is None
    assert tripwire.attributes["tokens_today"] == PROBE_TOKENS


# --- release ---


async def answer_again(hass: HomeAssistant, fake_client: MagicMock, entry: Any, noul: float) -> Any:
    """Evaluate the laundry watch once more with this probability, and read its verdict."""
    fake_client.ask.return_value = build_response(**{NOUL_KEY: NoulAnswer(noul=noul)})
    await entry.runtime_data.coordinators["laundry"].async_request_evaluation()
    await hass.async_block_till_done()
    return hass.states.get(VERDICT)


async def test_verdict_holds_between_release_and_cutoff(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    fake_client.ask.return_value = build_response(**{NOUL_KEY: NoulAnswer(noul=0.8)})
    entry = await yaml_watch(forgotten_only(cutoff=0.7, release=0.3))
    first = hass.states.get(VERDICT)
    assert (first.state, first.attributes["held"]) == ("on", False)
    assert first.attributes["release"] == 0.3
    walk = [(0.5, "on", True), (0.2, "off", False), (0.5, "off", True), (0.7, "on", False)]
    for noul, state, held in walk:
        verdict = await answer_again(hass, fake_client, entry, noul)
        assert (verdict.state, verdict.attributes["held"]) == (state, held), noul
        assert verdict.attributes["probability"] == noul


async def test_verdict_without_a_release_flips_at_the_cutoff_and_says_nothing_of_holding(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    fake_client.ask.return_value = build_response(**{NOUL_KEY: NoulAnswer(noul=0.8)})
    entry = await yaml_watch(forgotten_only(cutoff=0.7))
    verdict = await answer_again(hass, fake_client, entry, 0.69)
    assert verdict.state == "off"
    assert "held" not in verdict.attributes
    assert "release" not in verdict.attributes


async def test_verdict_first_answered_inside_the_band_is_unknown_and_left_out_of_the_event(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    events = async_capture_events(hass, EVENT_DECISION)
    fake_client.ask.return_value = build_response(**{NOUL_KEY: NoulAnswer(noul=0.5)})
    entry = await yaml_watch(forgotten_only(cutoff=0.7, release=0.3))
    verdict = hass.states.get(VERDICT)
    assert (verdict.state, verdict.attributes["held"]) == ("unknown", True)
    shown = events[-1].data["answers"][NOUL_KEY]
    assert "verdict" not in shown
    assert shown["held"] is True

    await answer_again(hass, fake_client, entry, 0.75)
    shown = events[-1].data["answers"][NOUL_KEY]
    assert (shown["verdict"], shown["held"]) == (True, False)


async def test_verdict_held_through_a_reply_that_left_the_noul_out(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    fake_client.ask.return_value = build_response(**{NOUL_KEY: NoulAnswer(noul=0.9)})
    entry = await yaml_watch(forgotten_only(cutoff=0.7, release=0.3))
    fake_client.ask.return_value = build_response()
    await entry.runtime_data.coordinators["laundry"].async_request_evaluation()
    await hass.async_block_till_done()
    assert hass.states.get(VERDICT).state == "unavailable"
    verdict = await answer_again(hass, fake_client, entry, 0.5)
    assert (verdict.state, verdict.attributes["held"]) == ("on", True)
