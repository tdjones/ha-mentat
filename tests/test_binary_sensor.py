"""A noul's verdict against its cutoff, and the budget tripwire."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from jevclient import NoulAnswer

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
