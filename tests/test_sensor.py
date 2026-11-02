"""Question sensors, the per-watch diagnostics and the day's ledger."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from jevclient import ChoiceAnswer, NoulAnswer
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.mentat import YAML_WATCHES
from custom_components.mentat.const import DOMAIN, UID_BYTES, UID_LATENCY
from custom_components.mentat.coordinator import MentatCoordinator
from custom_components.mentat.sensor import MentatWatchLatencySensor
from custom_components.mentat.watches import WatchConfig

from .conftest import (
    ANSWER_TOKENS,
    CHOICE_KEY,
    LATENCY_MS,
    NOUL_KEY,
    PROBE_TOKENS,
    SCORE_KEY,
    WATCH,
    LoadYaml,
    build_response,
    laundry_answers,
)

TYPESAFE_PRICE = 0.042


async def test_sensor_each_question_type_reads_its_answer(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    fake_client.ask.return_value = build_response(**laundry_answers())
    await yaml_watch()

    noul = hass.states.get("sensor.typesafe_laundry_forgotten")
    assert noul.state == "0.81"
    assert noul.attributes["question_type"] == "noul"
    assert noul.attributes["state_class"] == "measurement"

    choice = hass.states.get("sensor.typesafe_programme")
    assert choice.state == "spin"
    assert choice.attributes["device_class"] == "enum"
    assert choice.attributes["options"] == ["wash", "spin", "idle"]
    assert choice.attributes["confidence"] == 0.8
    assert choice.attributes["probabilities"] == {"wash": 0.1, "spin": 0.85, "idle": 0.05}

    score = hass.states.get("sensor.typesafe_urgency")
    assert score.state == "1.4"
    assert score.attributes["legend"] == {"0": "Not yet", "1": "Today", "2": "Now"}
    assert score.attributes["nearest_level"] == "Today"
    assert score.attributes["confidence"] == 0.6
    assert score.attributes["probabilities"] == {"0": 0.1, "1": 0.4, "2": 0.5}

    registered = er.async_get(hass).async_get("sensor.typesafe_laundry_forgotten")
    # A YAML question is keyed apart from the entry that runs it.
    assert registered.unique_id == f"yaml:{NOUL_KEY}"
    assert registered.translation_key == "noul"


async def test_sensor_answer_of_another_type_shows_unknown(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    """A server that answers a choice with a noul broke a promise, so nothing shows."""
    stray = ChoiceAnswer(choice="b", probabilities={"b": 1.0}, confidence=1.0)
    fake_client.ask.return_value = build_response(
        **{NOUL_KEY: stray, CHOICE_KEY: NoulAnswer(noul=0.4), SCORE_KEY: stray}
    )
    await yaml_watch()
    noul = hass.states.get("sensor.typesafe_laundry_forgotten")
    assert noul.state == STATE_UNKNOWN
    assert "question_type" not in noul.attributes
    assert hass.states.get("sensor.typesafe_programme").state == STATE_UNKNOWN
    assert hass.states.get("binary_sensor.typesafe_laundry_forgotten").state == STATE_UNKNOWN


async def test_sensor_choice_outside_its_options_shows_unknown(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    """An enum sensor cannot hold an option it does not list, so nothing shows."""
    rinse = ChoiceAnswer(choice="rinse", probabilities={"rinse": 0.9}, confidence=0.9)
    fake_client.ask.return_value = build_response(**(laundry_answers() | {CHOICE_KEY: rinse}))
    await yaml_watch()
    choice = hass.states.get("sensor.typesafe_programme")
    assert choice.state == STATE_UNKNOWN
    assert "confidence" not in choice.attributes
    assert hass.states.get("sensor.typesafe_laundry_forgotten").state == "0.81"


async def test_sensor_question_named_like_a_diagnostic_keeps_its_own_entity(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    """A question called Latency and the watch's latency diagnostic are two entities."""
    latency = {"name": "Latency", "type": "noul", "instructions": "Is the washer slow?"}
    fake_client.ask.return_value = build_response(laundry_latency=NoulAnswer(noul=0.81))
    await yaml_watch([{**WATCH, "questions": [latency]}])
    registry = er.async_get(hass)
    question = registry.async_get_entity_id("sensor", DOMAIN, "yaml:laundry_latency")
    diagnostic = registry.async_get_entity_id("sensor", DOMAIN, "yaml:watch:laundry:latency")
    assert question is not None
    assert diagnostic is not None
    assert question != diagnostic
    assert hass.states.get(question).state == "0.81"


async def test_sensor_watch_diagnostics_start_disabled_and_show_the_last_request(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    fake_client.ask.return_value = build_response(**laundry_answers())
    entry = await yaml_watch()
    registry = er.async_get(hass)
    found = {
        suffix: registry.async_get_entity_id("sensor", DOMAIN, f"yaml:watch:laundry:{suffix}")
        for suffix in (UID_LATENCY, UID_BYTES)
    }
    for entity_id in found.values():
        assert entity_id is not None
        assert registry.async_get(entity_id).disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert hass.states.get(entity_id) is None
        registry.async_update_entity(entity_id, disabled_by=None)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert found[UID_LATENCY] == "sensor.typesafe_laundry_latency"
    latency = hass.states.get(found[UID_LATENCY])
    assert latency.state == str(round(LATENCY_MS))
    assert latency.attributes["unit_of_measurement"] == "ms"
    last = entry.runtime_data.coordinators["laundry"].last
    assert last is not None
    sent = hass.states.get(found[UID_BYTES])
    assert sent.state == str(last.body_bytes)
    assert sent.attributes["sent_state"] == last.state
    assert sent.attributes["questions"][NOUL_KEY]["type"] == "noul"


async def test_sensor_watch_diagnostics_before_an_answer_are_empty_but_available(
    hass: HomeAssistant, yaml_watch: LoadYaml
) -> None:
    entry = await yaml_watch([])
    watch = WatchConfig(key="porch", name="Porch", questions=(), scan_interval=600)
    coordinator = MentatCoordinator(hass, entry, entry.runtime_data, watch)
    sensor = MentatWatchLatencySensor(coordinator)
    assert sensor.native_value is None
    assert sensor.available
    assert sensor.unique_id == f"{entry.entry_id}:watch:porch:{UID_LATENCY}"


async def test_sensor_ledger_counts_requests_tokens_and_spend(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    fake_client.ask.return_value = build_response(**laundry_answers())
    await yaml_watch()
    billed = PROBE_TOKENS + ANSWER_TOKENS
    assert hass.states.get("sensor.typesafe_requests_today").state == "2"
    tokens = hass.states.get("sensor.typesafe_tokens_today")
    assert tokens.state == str(billed)
    assert tokens.attributes["token_budget"] is None
    spend = hass.states.get("sensor.typesafe_spend_today")
    assert float(spend.state) == round(billed / 1e6 * TYPESAFE_PRICE, 6)
    assert spend.attributes["token_price"] == TYPESAFE_PRICE
    assert spend.attributes["last_reset"] == dt_util.start_of_local_day().isoformat()

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=302))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.typesafe_requests_today").state == "3"


async def test_sensor_removed_question_takes_its_entities_along(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    fake_client.ask.return_value = build_response(**laundry_answers())
    entry = await yaml_watch()
    registry = er.async_get(hass)
    assert registry.async_get("sensor.typesafe_programme") is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
    hass.data[YAML_WATCHES].raw = []
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get("sensor.typesafe_programme") is None
    assert registry.async_get("binary_sensor.typesafe_laundry_forgotten") is None
    assert hass.states.get("sensor.typesafe_requests_today").state != STATE_UNAVAILABLE
