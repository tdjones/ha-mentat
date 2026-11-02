"""AI Task fields as questions, and answers back as field values.

A script writer typed the structure, so every refusal names the field and says
what to change.
"""

from __future__ import annotations

from typing import Any

import pytest
import voluptuous as vol
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import selector
from jevclient import BoxAnswer, Choice, ChoiceAnswer, Noul, NoulAnswer, Score, ScoreAnswer

from custom_components.mentat.fields import (
    BOOLEAN_CUTOFF,
    RESERVED_KEY,
    Scale,
    questions_from_structure,
    values_from_answers,
)


def task_structure(**fields: tuple[str | None, dict[str, Any]]) -> vol.Schema:
    """What ai_task hands over: Required markers with descriptions, selector values."""
    return vol.Schema(
        {
            vol.Required(key, description=wording): selector.selector(config)
            for key, (wording, config) in fields.items()
        }
    )


def number(**config: Any) -> vol.Schema:
    return task_structure(level=("How far along?", {"number": config}))


def scored(fraction: float, rungs: int) -> ScoreAnswer:
    """A score sitting `fraction` of the way up `rungs` levels."""
    return ScoreAnswer(
        score=fraction * (rungs - 1),
        legend={str(rung): f"rung {rung}" for rung in range(rungs)},
        probabilities={str(rung): 1 / rungs for rung in range(rungs)},
        confidence=0.7,
    )


def refused(structure: vol.Schema) -> ServiceValidationError:
    with pytest.raises(ServiceValidationError) as caught:
        questions_from_structure(structure)
    return caught.value


# Questions


def test_boolean_field_asks_a_noul_of_its_description() -> None:
    structure = task_structure(gate_left_open=("Was the side gate left open?", {"boolean": {}}))
    assert questions_from_structure(structure) == {
        "gate_left_open": Noul("Was the side gate left open?")
    }


def test_select_field_asks_a_choice_over_distinct_values() -> None:
    structure = task_structure(
        bin_level=("How full is the bin?", {"select": {"options": ["empty", "half", "empty"]}})
    )
    assert questions_from_structure(structure) == {
        "bin_level": Choice("How full is the bin?", {"empty": None, "half": None})
    }


def test_select_field_with_labels_asks_about_the_values() -> None:
    """The value is what the script reads back, so the model picks a value."""
    options = [
        {"value": "away", "label": "Nobody home"},
        {"value": "home", "label": "Someone home"},
    ]
    structure = task_structure(presence=("Who is in?", {"select": {"options": options}}))
    assert list(questions_from_structure(structure)["presence"].criteria) == ["away", "home"]


def test_number_field_asks_a_score_with_unit_labels() -> None:
    structure = task_structure(
        humidity=(
            "How damp is the cellar?",
            {"number": {"min": 10, "max": 30, "step": 5, "unit_of_measurement": "%"}},
        )
    )
    assert questions_from_structure(structure) == {
        "humidity": Score("How damp is the cellar?", ["10%", "15%", "20%", "25%", "30%"])
    }


@pytest.mark.parametrize(
    ("config", "levels"),
    [
        # 51 whole numbers is more than a score takes, so ten, each on the step.
        (
            {"min": 0, "max": 50, "step": 1},
            ["0", "6", "11", "17", "22", "28", "33", "39", "44", "50"],
        ),
        ({"min": 3, "max": 4, "step": 1}, ["3", "4"]),
        ({"min": 20, "max": 21, "step": 0.25}, ["20", "20.25", "20.5", "20.75", "21"]),
        # The top rung snaps past 10 and is held at the end of the range.
        ({"min": 0, "max": 10, "step": 4}, ["0", "4", "10"]),
    ],
)
def test_number_field_levels_follow_the_step(config: dict[str, Any], levels: list[str]) -> None:
    question = questions_from_structure(number(**config))["level"]
    assert isinstance(question, Score)
    assert list(question.criteria) == levels


def test_number_field_without_a_step_gets_the_most_levels() -> None:
    question = questions_from_structure(number(min=0, max=2, step="any"))["level"]
    assert isinstance(question, Score)
    assert len(question.criteria) == 10


def test_field_without_a_description_asks_about_its_key() -> None:
    structure = task_structure(door_locked=(None, {"boolean": {}}))
    assert questions_from_structure(structure)["door_locked"] == Noul("door_locked")


def test_field_under_a_plain_string_key_is_read() -> None:
    structure = vol.Schema({"door_locked": selector.selector({"boolean": {}})})
    assert questions_from_structure(structure) == {"door_locked": Noul("door_locked")}


def test_field_named_like_the_result_key_is_refused() -> None:
    caught = refused(task_structure(**{RESERVED_KEY: ("Anything?", {"boolean": {}})}))
    assert caught.translation_key == "reserved_field"
    assert caught.translation_placeholders == {"field": "mentat"}


def test_structure_with_nothing_in_it_is_refused() -> None:
    assert refused(vol.Schema({})).translation_key == "no_fields"


@pytest.mark.parametrize(
    ("config", "selector_name"),
    [
        ({"text": {}}, "TextSelector"),
        ({"entity": {}}, "EntitySelector"),
        ({"object": {}}, "ObjectSelector"),
        ({"time": {}}, "TimeSelector"),
    ],
)
def test_field_no_decision_model_fills_is_refused(
    config: dict[str, Any], selector_name: str
) -> None:
    caught = refused(
        task_structure(locked=("Locked?", {"boolean": {}}), summary=("Say what you see", config))
    )
    assert caught.translation_key == "unsupported_field"
    assert caught.translation_placeholders == {"field": "summary", "selector": selector_name}


@pytest.mark.parametrize("options", [["solo"], [f"o{n}" for n in range(300)]], ids=["1", "300"])
def test_select_field_outside_the_option_range_is_refused(options: list[str]) -> None:
    caught = refused(task_structure(room=("Which room?", {"select": {"options": options}})))
    assert caught.translation_key == "field_options_out_of_range"
    assert caught.translation_placeholders == {
        "field": "room",
        "count": str(len(options)),
        "min": "2",
        "max": "255",
    }


@pytest.mark.parametrize(
    ("config", "missing"),
    [({"max": 9}, "min"), ({"min": 2}, "max"), ({"mode": "box", "step": 2}, "min")],
)
def test_number_field_without_an_end_names_it(config: dict[str, Any], missing: str) -> None:
    caught = refused(number(**config))
    assert caught.translation_key == "field_needs_scale"
    assert caught.translation_placeholders == {"field": "level", "bound": missing}


def test_number_field_running_downward_is_refused() -> None:
    caught = refused(number(min=7, max=3))
    assert caught.translation_key == "field_scale_not_ordered"
    assert caught.translation_placeholders == {"field": "level", "min": "7", "max": "3"}


@pytest.mark.parametrize(
    ("config", "step"),
    [({"min": 0, "max": 2, "step": 7}, "7"), ({"min": 5, "max": 6, "step": 2.5}, "2.5")],
)
def test_number_field_with_a_step_past_its_range_is_refused(
    config: dict[str, Any], step: str
) -> None:
    caught = refused(number(**config))
    assert caught.translation_key == "field_step_too_coarse"
    assert caught.translation_placeholders == {
        "field": "level",
        "min": f"{config['min']:g}",
        "max": f"{config['max']:g}",
        "step": step,
    }


# Values


@pytest.mark.parametrize(
    ("noul", "verdict"), [(BOOLEAN_CUTOFF, True), (0.4999, False), (0.73, True), (0.02, False)]
)
def test_boolean_value_is_true_from_the_cutoff(noul: float, verdict: bool) -> None:
    structure = task_structure(locked=("Locked?", {"boolean": {}}))
    assert values_from_answers(structure, {"locked": NoulAnswer(noul=noul)}) == {"locked": verdict}


def test_select_value_is_the_chosen_option() -> None:
    structure = task_structure(room=("Which room?", {"select": {"options": ["kitchen", "garage"]}}))
    answer = ChoiceAnswer(
        choice="garage", probabilities={"kitchen": 0.35, "garage": 0.65}, confidence=0.4
    )
    assert values_from_answers(structure, {"room": answer}) == {"room": "garage"}


@pytest.mark.parametrize(
    ("config", "fraction", "rungs", "value"),
    [
        ({"min": 10, "max": 30, "step": 5}, 0.0, 5, 10),
        ({"min": 10, "max": 30, "step": 5}, 0.5, 5, 20),
        ({"min": 10, "max": 30, "step": 5}, 1.0, 5, 30),
        ({"min": 10, "max": 30, "step": 5}, 0.6, 5, 20),
        ({"min": 10, "max": 30, "step": 5}, 0.65, 5, 25),
        # Ten rungs over 0..50 still land on a whole number.
        ({"min": 0, "max": 50, "step": 1}, 5 / 9, 10, 28),
        ({"min": 20, "max": 21, "step": 0.25}, 0.3, 5, 20.25),
        # No step: four decimal places and no snapping.
        ({"min": 0, "max": 2, "step": "any"}, 0.123456, 10, 0.2469),
        # Exactly half a step goes up. round() would give the even 2 and 4.
        ({"min": 0, "max": 8, "step": 1}, 0.3125, 9, 3),
        ({"min": 0, "max": 8, "step": 1}, 0.5625, 9, 5),
    ],
)
def test_number_value_lands_on_the_field_scale(
    config: dict[str, Any], fraction: float, rungs: int, value: float
) -> None:
    got = values_from_answers(number(**config), {"level": scored(fraction, rungs)})["level"]
    assert got == pytest.approx(value)
    assert type(got) is type(value)


def test_scale_value_is_held_inside_the_range() -> None:
    assert Scale(0.0, 10.0, 4.0).value(1.0) == 10


def test_scale_from_a_selector_reads_any_as_no_step() -> None:
    instance = selector.NumberSelector({"min": 1, "max": 9, "step": "any"})
    assert Scale.from_selector("level", instance) == Scale(1.0, 9.0, 0.0)


def test_missing_answer_is_named_not_left_none() -> None:
    structure = task_structure(locked=("Locked?", {"boolean": {}}))
    with pytest.raises(ServiceValidationError) as caught:
        values_from_answers(structure, {"other": NoulAnswer(noul=0.9)})
    assert caught.value.translation_key == "answer_missing"
    assert caught.value.translation_placeholders == {"field": "locked"}


def test_answer_of_another_type_names_field_answer_and_selector() -> None:
    structure = task_structure(room=("Which room?", {"select": {"options": ["kitchen", "hall"]}}))
    stray = BoxAnswer(top=0.1, bottom=0.4, left=0.2, right=0.6, confidence=0.3)
    with pytest.raises(ServiceValidationError) as caught:
        values_from_answers(structure, {"room": stray})
    assert caught.value.translation_key == "answer_does_not_fit"
    assert caught.value.translation_placeholders == {
        "field": "room",
        "answer": "BoxAnswer",
        "selector": "SelectSelector",
    }


def test_answer_for_a_field_no_question_covers_does_not_fit() -> None:
    """questions_from_structure refuses such a field first. This is the fallback."""
    structure = task_structure(summary=("Say what you see", {"text": {}}))
    with pytest.raises(ServiceValidationError) as caught:
        values_from_answers(structure, {"summary": NoulAnswer(noul=0.5)})
    assert caught.value.translation_key == "answer_does_not_fit"
    assert caught.value.translation_placeholders["selector"] == "TextSelector"
