"""The examples load through the same schemas the integration and the actions use."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import voluptuous as vol
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.ai_task import (
    STRUCTURE_FIELD_SCHEMA,
    _validate_structure_fields,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util
from homeassistant.util.yaml import load_yaml
from jevclient import Choice, Noul, Score

from custom_components.mentat.fields import questions_from_structure
from custom_components.mentat.schema import CONFIG_SCHEMA
from custom_components.mentat.services import ASK_SCHEMA
from custom_components.mentat.watches import question_from_mapping, watch_from_yaml

EXAMPLES = Path(__file__).parent.parent / "examples"


def _actions(automation: dict[str, Any], action: str) -> list[dict[str, Any]]:
    return [step for step in automation["actions"] if step.get("action") == action]


async def test_house_mode_is_a_valid_window_watch(hass: HomeAssistant) -> None:
    config = CONFIG_SCHEMA(load_yaml(EXAMPLES / "house_mode.yaml"))
    [raw] = config["mentat"]
    watch = watch_from_yaml(hass, raw)
    assert watch.key == "house_mode"
    assert watch.scan_interval == 600
    window = watch.window
    assert window is not None
    assert window.duration == timedelta(hours=1)
    assert (window.max_events, window.settle, window.evaluate) == (150, 20.0, "both")
    [camera] = window.events
    assert camera.data == {"cameraName": "front_door"}
    assert watch.target is not None
    assert len(watch.target["entity_id"]) == 20

    mode, in_bed, empty = watch.questions
    assert isinstance(mode.question, Choice)
    assert mode.choices == ("asleep", "winding_down", "active", "waking", "away", "other")
    # The household rules arrive under the key the instructions name.
    assert "household_rules" in mode.question.instructions
    assert isinstance(in_bed.question, Noul)
    assert in_bed.question.true == "Everyone who counts and is home is in bed"
    assert (in_bed.cutoff, empty.cutoff) == (0.6, 0.5)


async def test_the_house_mode_note_works_out_who_counts(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await hass.config.async_set_time_zone("UTC")
    [raw] = CONFIG_SCHEMA(load_yaml(EXAMPLES / "house_mode.yaml"))["mentat"]
    watch = watch_from_yaml(hass, raw)
    freezer.move_to("2026-10-30T22:00:00+00:00")
    hass.states.async_set("person.alice", "home")
    hass.states.async_set("person.bob", "home")
    hass.states.async_set("person.carol", "home")
    hass.states.async_set("binary_sensor.alice_phone_charging", "on")
    hass.states.async_set("binary_sensor.bob_phone_charging", "off")
    hass.states.async_set("binary_sensor.carol_phone_charging", "on")
    hass.states.async_set("sun.sun", "below_horizon")
    freezer.move_to(dt_util.utcnow() + timedelta(minutes=25))
    assert watch.note is not None
    note = " ".join(watch.note.async_render(parse_result=False).split())
    assert note.startswith("Time of day: late evening; the sun is down.")
    assert "1 of 2 people who count for bedtime" in note
    assert note.endswith("Carol is home but does not count for bedtime while an adult is home.")


def _package_automations() -> list[dict[str, Any]]:
    automations = load_yaml(EXAMPLES / "package_check.yaml")
    assert isinstance(automations, list)
    return automations


def test_the_package_check_asks_two_questions_about_three_pictures() -> None:
    automation = next(a for a in _package_automations() if a["id"] == "porch_package_check_mentat")
    [step] = _actions(automation, "mentat.ask")
    data = ASK_SCHEMA(step["data"])
    assert data["pictures"][0] == "/media/porch/baseline_day.jpg"
    assert data["pictures"][1] == "{{ trigger.event.data.images[0] }}"
    questions = {key: question_from_mapping(raw) for key, raw in data["questions"].items()}
    package_left, what_changed = questions["package_left"], questions["what_changed"]
    assert isinstance(package_left, Noul)
    assert package_left.true is not None
    assert package_left.false is not None
    assert "never packages" in str(package_left.false)
    assert isinstance(what_changed, Choice)
    assert list(what_changed.criteria) == ["package", "person", "other_object", "nothing"]


def test_the_ai_task_variant_maps_to_a_noul_and_a_score() -> None:
    automation = next(a for a in _package_automations() if a["id"] == "porch_package_check_ai_task")
    [step] = _actions(automation, "ai_task.generate_data")
    structure = vol.All(vol.Schema({str: STRUCTURE_FIELD_SCHEMA}), _validate_structure_fields)(
        step["data"]["structure"]
    )
    questions = questions_from_structure(structure)
    assert isinstance(questions["detected"], Noul)
    confidence = questions["confidence"]
    assert isinstance(confidence, Score)
    assert confidence.criteria == ["0", "1", "2", "3", "4"]
    attachments = selector.MediaSelector({"accept": ["*/*"], "multiple": True})(
        step["data"]["attachments"]
    )
    assert len(attachments) == 3


async def test_the_package_check_automations_are_valid(hass: HomeAssistant) -> None:
    """Their template conditions use the shorthand form, which HA validates too."""
    from homeassistant.components.automation.config import async_validate_config_item

    for automation in _package_automations():
        validated = await async_validate_config_item(hass, automation["id"], automation)
        assert validated is not None
        assert validated.validation_error is None, validated.validation_error
