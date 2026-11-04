"""The examples load through the same schemas the integration and the actions use."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
import voluptuous as vol
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.ai_task import (
    STRUCTURE_FIELD_SCHEMA,
    _validate_structure_fields,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector
from homeassistant.helpers.template import Template
from homeassistant.util import dt as dt_util
from homeassistant.util.yaml import load_yaml, parse_yaml
from jevclient import Box, Choice, ChoiceAnswer, Noul, NoulAnswer, Score, ScoreAnswer
from pytest_homeassistant_custom_component.common import (
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.mentat.ai_task import _receipt
from custom_components.mentat.const import EVENT_DECISION
from custom_components.mentat.fields import questions_from_structure, values_from_answers
from custom_components.mentat.schema import CONFIG_SCHEMA
from custom_components.mentat.services import (
    ASK_SCHEMA,
    BOX_SCHEMA,
    CHOICE_SCHEMA,
    NOUL_SCHEMA,
    SCORE_SCHEMA,
)
from custom_components.mentat.watches import (
    question_from_mapping,
    watch_from_yaml,
    window_from_mapping,
)

from .conftest import LoadYaml, build_response

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


def _commented(path: Path, first: str) -> Any:
    """What a user pastes from a commented block, from its first line to the end."""
    lines = path.read_text().splitlines()
    start = lines.index(f"# {first}")
    return parse_yaml("\n".join(line.removeprefix("#").removeprefix(" ") for line in lines[start:]))


def _render(template: str, hass: HomeAssistant, **variables: Any) -> Any:
    return Template(template, hass).async_render(variables)


async def advance(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


LAUNDRY = EXAMPLES / "laundry.yaml"
LAUNDRY_ENTITIES = [
    "sensor.washing_machine_power",
    "sensor.dryer_power",
    "binary_sensor.laundry_door",
]


async def test_laundry_is_a_plain_watch_of_three_questions(hass: HomeAssistant) -> None:
    [raw] = CONFIG_SCHEMA(load_yaml(LAUNDRY))["mentat"]
    watch = watch_from_yaml(hass, raw)
    assert (watch.key, watch.scan_interval, watch.window) == ("laundry", 600, None)
    assert watch.target == {"entity_id": LAUNDRY_ENTITIES}
    assert watch.wake_on == ("binary_sensor.laundry_door",)
    assert [spec.key for spec in watch.questions] == [
        "laundry_load_waiting",
        "laundry_machine_stage",
        "laundry_unload_urgency",
    ]

    waiting, stage, urgency = watch.questions
    assert isinstance(waiting.question, Noul)
    assert waiting.question.true == "A machine has finished and nobody has unloaded it"
    assert waiting.question.false is not None
    # The brief goes with the question, not into the state.
    assert "soaks" in waiting.question.instructions["background"]
    assert (waiting.cutoff, waiting.wants_verdict) == (0.6, True)
    assert isinstance(stage.question, Choice)
    assert stage.choices == ("washing", "drying", "both", "finished", "idle")
    assert stage.question.criteria["idle"] is None
    assert isinstance(urgency.question, Score)
    assert urgency.question.criteria == [
        "Nothing to unload",
        "Unload when convenient",
        "Unload within the hour",
        "Unload now",
    ]
    assert not (stage.wants_verdict or urgency.wants_verdict)


async def test_the_laundry_note_counts_idle_minutes(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await hass.config.async_set_time_zone("UTC")
    [raw] = CONFIG_SCHEMA(load_yaml(LAUNDRY))["mentat"]
    note = watch_from_yaml(hass, raw).note
    assert note is not None

    def read() -> str:
        return " ".join(note.async_render(parse_result=False).split())

    freezer.move_to("2026-10-30T09:00:00+00:00")
    hass.states.async_set("binary_sensor.laundry_door", "off")
    hass.states.async_set("sensor.dryer_power", "2150")
    freezer.move_to("2026-10-30T10:10:00+00:00")
    hass.states.async_set("sensor.washing_machine_power", "1.4")
    freezer.move_to("2026-10-30T10:35:00+00:00")
    assert read() == (
        "The washer has been idle for 25 minutes. The dryer is running. "
        "Nobody has opened the laundry door since the washer stopped."
    )

    hass.states.async_set("binary_sensor.laundry_door", "on")
    hass.states.async_set("sensor.dryer_power", "unavailable")
    assert read() == (
        "The washer has been idle for 25 minutes. The dryer power reading is unavailable."
    )


def _laundry_answers(urgency: float) -> dict[str, Any]:
    levels = ["Nothing to unload", "Unload when convenient", "Unload within the hour", "Unload now"]
    return {
        "laundry_load_waiting": NoulAnswer(noul=0.82),
        "laundry_machine_stage": ChoiceAnswer(
            choice="finished",
            probabilities={
                "washing": 0.05,
                "drying": 0.05,
                "both": 0.0,
                "finished": 0.8,
                "idle": 0.1,
            },
            confidence=0.7,
        ),
        "laundry_unload_urgency": ScoreAnswer(
            score=urgency,
            legend={str(rung): level for rung, level in enumerate(levels)},
            probabilities={"0": 0.1, "1": 0.2, "2": 0.4, "3": 0.3},
            confidence=0.5,
        ),
    }


async def test_the_laundry_automation_reads_what_the_watch_fires(
    hass: HomeAssistant, fake_client: MagicMock, yaml_watch: LoadYaml
) -> None:
    from homeassistant.components.automation.config import async_validate_config_item

    [automation] = _commented(LAUNDRY, "automation:")["automation"]
    validated = await async_validate_config_item(hass, "laundry_is_waiting", automation)
    assert validated is not None
    assert validated.validation_error is None, validated.validation_error
    assert automation["triggers"][0]["event_data"] == {"watch": "laundry"}
    [condition] = automation["conditions"]
    [notify] = _actions(automation, "notify.notify")

    for entity_id, state in zip(LAUNDRY_ENTITIES, ("1.4", "0.3", "off"), strict=True):
        hass.states.async_set(entity_id, state)
    fake_client.ask.return_value = build_response(**_laundry_answers(urgency=2.1))
    events = async_capture_events(hass, EVENT_DECISION)
    await yaml_watch(load_yaml(LAUNDRY)["mentat"])
    assert hass.states.get("sensor.typesafe_load_waiting").state == "0.82"
    assert hass.states.get("binary_sensor.typesafe_load_waiting").state == "on"
    assert hass.states.get("sensor.typesafe_machine_stage").state == "finished"
    assert hass.states.get("sensor.typesafe_unload_urgency").state == "2.1"

    [event] = events
    assert event.data["watch"] == "laundry"
    trigger = {"event": {"data": event.data}}
    assert _render(condition, hass, trigger=trigger) is True
    assert _render(notify["data"]["message"], hass, trigger=trigger) == (
        "The laundry is waiting to be unloaded (82% sure)."
    )
    # A lower score, or a score the reply left out, sends nothing.
    answers = event.data["answers"]
    calm = answers | {
        "laundry_unload_urgency": answers["laundry_unload_urgency"] | {"normalized": 0.33}
    }
    assert _render(condition, hass, trigger={"event": {"data": {"answers": calm}}}) is False
    partial = {"laundry_load_waiting": answers["laundry_load_waiting"]}
    assert _render(condition, hass, trigger={"event": {"data": {"answers": partial}}}) is False

    # A power reading does not wake the watch; the door does.
    asked = fake_client.ask.await_count
    hass.states.async_set("sensor.washing_machine_power", "0.9")
    await advance(hass, 2)
    assert fake_client.ask.await_count == asked
    hass.states.async_set("binary_sensor.laundry_door", "on")
    await advance(hass, 2)
    assert fake_client.ask.await_count == asked + 1


ACTION_SCHEMAS: dict[str, vol.Schema] = {
    "mentat.noul": NOUL_SCHEMA,
    "mentat.choice": CHOICE_SCHEMA,
    "mentat.score": SCORE_SCHEMA,
    "mentat.ask": ASK_SCHEMA,
    "mentat.box": BOX_SCHEMA,
}


def _scripts() -> dict[str, Any]:
    scripts = load_yaml(EXAMPLES / "actions.yaml")
    assert isinstance(scripts, dict)
    return scripts


def _call(object_id: str, action: str) -> dict[str, Any]:
    """A script's call to `action` as the action's schema sees it, target and all."""
    [step] = [step for step in _scripts()[object_id]["sequence"] if step.get("action") == action]
    return cast(dict[str, Any], ACTION_SCHEMAS[action]({**step.get("target", {}), **step["data"]}))


async def test_every_action_script_is_valid(hass: HomeAssistant) -> None:
    from homeassistant.components.script.config import async_validate_config_item

    used: set[str] = set()
    for object_id, script in _scripts().items():
        validated = await async_validate_config_item(hass, object_id, script)
        assert validated is not None
        assert validated.validation_error is None, validated.validation_error
        for step in script["sequence"]:
            action = step.get("action", "")
            if action.startswith("mentat."):
                # Every action answers only as a response.
                assert step["response_variable"]
                ACTION_SCHEMAS[action]({**step.get("target", {}), **step["data"]})
                used.add(action)
    assert used == set(ACTION_SCHEMAS)


def test_the_noul_script_sets_its_own_cutoff() -> None:
    data = _call("laundry_done", "mentat.noul")
    assert data["entity_id"] == ["sensor.washing_machine_power", "binary_sensor.laundry_door"]
    assert data["cutoff"] == 0.7
    question = question_from_mapping({**data, "type": "noul"})
    assert isinstance(question, Noul)
    assert question.true == "The wash is done and nobody has emptied it"
    assert "5 W" in question.instructions["background"]


async def test_the_choice_script_takes_options_as_a_mapping(hass: HomeAssistant) -> None:
    data = _call("doorbell_who", "mentat.choice")
    assert data["pictures"] == ["camera.front_door"]
    question = question_from_mapping({**data, "type": "choice"})
    assert isinstance(question, Choice)
    assert list(question.criteria) == ["delivery", "visitor", "nobody"]
    assert question.criteria["nobody"] is None
    hass.states.async_set("input_boolean.delivery_expected", "on")
    note = " ".join(Template(data["note"], hass).async_render(parse_result=False).split())
    assert note.endswith("A delivery is expected today.")


def test_the_score_script_reads_an_area() -> None:
    data = _call("garage_urgency", "mentat.score")
    assert data["area_id"] == ["garage"]
    question = question_from_mapping({**data, "type": "score"})
    assert isinstance(question, Score)
    assert question.criteria[0] == "Nothing to do"
    assert len(question.criteria) == 4


def test_the_ask_script_reads_half_an_hour_of_the_front_door() -> None:
    data = _call("front_door_left", "mentat.ask")
    assert len(data["entity_id"]) == 3
    assert window_from_mapping(data["window"]).duration == timedelta(minutes=30)
    questions = {key: question_from_mapping(raw) for key, raw in data["questions"].items()}
    assert isinstance(questions["left_unlocked"], Noul)
    assert isinstance(questions["traffic"], Score)


def test_the_driveway_scripts_ask_whether_before_where() -> None:
    data = _call("driveway_car", "mentat.ask")
    assert data["pictures"] == ["camera.driveway"]
    questions = {key: question_from_mapping(raw) for key, raw in data["questions"].items()}
    assert isinstance(questions["car_present"], Noul)
    assert isinstance(questions["car"], Box)

    box = _call("driveway_car_position", "mentat.box")
    assert box["picture"] == "camera.driveway"
    # mentat.box names one picture; the plural every other action takes is refused.
    with pytest.raises(vol.Invalid):
        BOX_SCHEMA({**box, "pictures": ["camera.driveway"]})


async def test_the_garage_check_fills_a_boolean_a_select_and_a_number(
    hass: HomeAssistant,
) -> None:
    from homeassistant.components.automation.config import async_validate_config_item

    automations = load_yaml(EXAMPLES / "ai_task.yaml")
    assert isinstance(automations, list)
    [automation] = automations
    validated = await async_validate_config_item(hass, automation["id"], automation)
    assert validated is not None
    assert validated.validation_error is None, validated.validation_error

    [step] = _actions(automation, "ai_task.generate_data")
    assert step["data"]["entity_id"] == "ai_task.mentat"
    structure = vol.All(vol.Schema({str: STRUCTURE_FIELD_SCHEMA}), _validate_structure_fields)(
        step["data"]["structure"]
    )
    questions = questions_from_structure(structure)
    assert list(questions) == ["door_open", "parked", "clutter"]
    assert isinstance(questions["door_open"], Noul)
    parked = questions["parked"]
    assert isinstance(parked, Choice)
    # The model chooses among the values; the labels never reach it.
    assert list(parked.criteria) == ["car", "bikes", "empty"]
    clutter = questions["clutter"]
    assert isinstance(clutter, Score)
    assert clutter.criteria == ["0", "2", "4", "6", "8", "10"]
    [attachment] = selector.MediaSelector({"accept": ["*/*"], "multiple": True})(
        step["data"]["attachments"]
    )
    assert attachment["media_content_id"] == "media-source://camera/camera.garage"

    # What the automation reads back: each value in its field's type, and the receipt.
    def result(door: float) -> dict[str, Any]:
        response = build_response(
            door_open=NoulAnswer(noul=door),
            parked=ChoiceAnswer(
                choice="car", probabilities={"car": 0.8, "bikes": 0.1, "empty": 0.1}, confidence=0.8
            ),
            clutter=ScoreAnswer(
                score=2.6,
                legend={str(rung): str(rung * 2) for rung in range(6)},
                probabilities={str(rung): 1 / 6 for rung in range(6)},
                confidence=0.4,
            ),
        )
        values = values_from_answers(structure, response.answers)
        return {"data": values | {"mentat": _receipt(response)}}

    sure = result(0.91)["data"]
    assert {key: sure[key] for key in questions} == {
        "door_open": True,
        "parked": "car",
        "clutter": 6,
    }
    # The bare true hides how sure the model was; the receipt does not.
    assert sure["mentat"]["answers"]["door_open"] == {"probability": 0.91}
    [check] = [step for step in automation["actions"] if "if" in step]
    [condition] = check["if"]
    [notify] = check["then"]
    assert _render(condition, hass, result=result(0.91)) is True
    assert _render(notify["data"]["message"], hass, result=result(0.91)) == (
        "The garage door is open (91% sure). Parked: car; clutter 6 of 10."
    )
    # True at 0.5, but not sure enough to nag.
    assert result(0.7)["data"]["door_open"] is True
    assert _render(condition, hass, result=result(0.7)) is False
