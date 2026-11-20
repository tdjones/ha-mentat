"""Watches built from YAML and grouped from UI questions, and answers as plain data."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from jevclient import (
    Box,
    BoxAnswer,
    Choice,
    ChoiceAnswer,
    Noul,
    NoulAnswer,
    Score,
    ScoreAnswer,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mentat.const import DOMAIN, SUBENTRY_QUESTION
from custom_components.mentat.schema import WATCH_SCHEMA, WINDOW_SCHEMA
from custom_components.mentat.watches import (
    EventFilter,
    WindowConfig,
    answer_as_dict,
    box_edges,
    fold_brief,
    is_own_event,
    question_from_mapping,
    question_spec,
    share_key,
    watch_from_yaml,
    watches_from_subentries,
    window_from_mapping,
)

from .conftest import ENTRY_DATA

DISHWASHER = "sensor.dishwasher_power"
BACK_DOOR = "binary_sensor.back_door"


# --- the brief ---


def test_fold_brief_without_a_brief_returns_the_instructions() -> None:
    assert fold_brief("Has the cycle ended?", None) == "Has the cycle ended?"
    assert fold_brief({"ask": "Ended?"}, "") == {"ask": "Ended?"}


def test_fold_brief_text_files_under_background() -> None:
    assert fold_brief("Has the cycle ended?", "Rinse draws 40 W") == {
        "question": "Has the cycle ended?",
        "background": "Rinse draws 40 W",
    }


def test_fold_brief_mapping_merges_key_by_key() -> None:
    assert fold_brief({"ask": "Ended?"}, {"rinse_watts": 40, "dry_minutes": 25}) == {
        "ask": "Ended?",
        "rinse_watts": 40,
        "dry_minutes": 25,
    }


def test_fold_brief_text_onto_mapping_instructions_adds_background() -> None:
    assert fold_brief({"ask": "Ended?"}, "Rinse draws 40 W") == {
        "ask": "Ended?",
        "background": "Rinse draws 40 W",
    }


# --- one question ---


def test_question_from_mapping_noul_reads_high_and_low() -> None:
    noul = question_from_mapping(
        {"type": "noul", "instructions": "Clean?", "high": "Dry and cool", "low": ""}
    )
    assert isinstance(noul, Noul)
    assert (noul.true, noul.false) == ("Dry and cool", None)


def test_question_from_mapping_folds_the_brief_in() -> None:
    noul = question_from_mapping({"type": "noul", "instructions": "Clean?", "brief": "Eco mode"})
    assert noul.instructions == {"question": "Clean?", "background": "Eco mode"}


def test_question_from_mapping_choice_takes_criteria() -> None:
    choice = question_from_mapping(
        {"type": "choice", "instructions": "Stage?", "criteria": {"rinse": "Water", "dry": None}}
    )
    assert isinstance(choice, Choice)
    assert dict(choice.criteria) == {"rinse": "Water", "dry": None}


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        (["rinse", "dry"], {"rinse": None, "dry": None}),
        ({"rinse": "Water", "dry": ""}, {"rinse": "Water", "dry": None}),
        ("rinse: water running\ndry: fan on", {"rinse": "water running", "dry": "fan on"}),
        ("rinse\n\n   dry  :  fan on  \n", {"rinse": None, "dry": "fan on"}),
        ("rinse:\ndry", {"rinse": None, "dry": None}),
    ],
)
def test_question_from_mapping_options_in_every_form(options: Any, expected: Any) -> None:
    choice = question_from_mapping({"type": "choice", "instructions": "Stage?", "options": options})
    assert isinstance(choice, Choice)
    assert dict(choice.criteria) == expected


@pytest.mark.parametrize(
    ("scale", "expected"),
    [
        (["Cold", "Warm", "Hot"], ["Cold", "Warm", "Hot"]),
        ("Cold\n\nWarm\nHot\n", ["Cold", "Warm", "Hot"]),
    ],
)
def test_question_from_mapping_scale_keeps_its_order(scale: Any, expected: list[str]) -> None:
    score = question_from_mapping({"type": "score", "instructions": "How hot?", "scale": scale})
    assert isinstance(score, Score)
    assert list(score.criteria) == expected


def test_question_from_mapping_score_takes_criteria() -> None:
    score = question_from_mapping(
        {"type": "score", "instructions": "How hot?", "criteria": ["Cold", "Hot"]}
    )
    assert isinstance(score, Score)
    assert list(score.criteria) == ["Cold", "Hot"]


def test_question_from_mapping_box_needs_only_instructions() -> None:
    assert isinstance(question_from_mapping({"type": "box", "instructions": "the mug"}), Box)


def test_question_from_mapping_unknown_type_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown question type 'haiku'"):
        question_from_mapping({"type": "haiku", "instructions": "Clean?"})


def test_question_spec_noul_with_cutoff_wants_a_verdict() -> None:
    spec = question_spec({"name": "Clean", "type": "noul", "instructions": "x", "cutoff": 0.0}, "q")
    assert (spec.key, spec.name, spec.kind, spec.cutoff) == ("q", "Clean", "noul", 0.0)
    assert spec.wants_verdict
    assert spec.choices == ()


def test_question_spec_noul_without_cutoff_wants_no_verdict() -> None:
    spec = question_spec({"name": "Clean", "type": "noul", "instructions": "x", "cutoff": ""}, "q")
    assert spec.cutoff is None
    assert not spec.wants_verdict


def test_question_spec_choice_lists_its_choices_in_order() -> None:
    spec = question_spec(
        {
            "name": "Stage",
            "type": "choice",
            "instructions": "x",
            "options": ["rinse", "dry", "off"],
        },
        "s",
    )
    assert not spec.wants_verdict
    assert spec.choices == ("rinse", "dry", "off")


# --- answers ---


def test_answer_as_dict_noul_is_type_and_noul() -> None:
    assert answer_as_dict(NoulAnswer(noul=0.62)) == {"type": "noul", "noul": 0.62}


def test_answer_as_dict_choice_carries_its_spread() -> None:
    answer = ChoiceAnswer(choice="dry", probabilities={"rinse": 0.3, "dry": 0.7}, confidence=0.55)
    assert answer_as_dict(answer) == {
        "type": "choice",
        "choice": "dry",
        "confidence": 0.55,
        "probabilities": {"rinse": 0.3, "dry": 0.7},
    }


def test_answer_as_dict_score_normalises_against_the_top_level() -> None:
    answer = ScoreAnswer(
        score=2.0,
        legend={"0": "Cold", "1": "Warm", "2": "Hot", "3": "Scalding"},
        probabilities={"0": 0.05, "1": 0.15, "2": 0.6, "3": 0.2},
        confidence=0.45,
    )
    flat = answer_as_dict(answer)
    assert flat["type"] == "score"
    assert (flat["score"], flat["normalized"], flat["nearest_level"]) == (2.0, 0.6667, "Hot")
    assert (flat["confidence"], flat["legend"]["3"]) == (0.45, "Scalding")
    assert flat["probabilities"]["2"] == 0.6


def test_answer_as_dict_box_is_its_edges() -> None:
    answer = BoxAnswer(top=0.25, bottom=0.75, left=0.4, right=0.8, confidence=0.7)
    assert answer_as_dict(answer) == {"type": "box", **box_edges(answer)}
    assert box_edges(answer) == {
        "left": 0.4,
        "top": 0.25,
        "right": 0.8,
        "bottom": 0.75,
        "confidence": 0.7,
    }


def test_is_own_event_matches_the_prefix_only() -> None:
    assert is_own_event("mentat_decision")
    assert is_own_event("mentat_later")
    assert not is_own_event("dishwasher_done")


# --- windows ---


def test_window_from_mapping_fills_the_defaults() -> None:
    window = window_from_mapping(
        WINDOW_SCHEMA({"duration": "00:45:00", "events": [{"event_type": "kettle_on"}]})
    )
    assert window == WindowConfig(
        duration=timedelta(minutes=45),
        max_events=100,
        settle=2.0,
        evaluate="both",
        events=(EventFilter(event_type="kettle_on", data={}, label=None),),
        with_attributes=False,
        collapse_repeats=True,
    )


def test_window_from_mapping_keeps_event_data_and_label() -> None:
    window = window_from_mapping(
        WINDOW_SCHEMA(
            {
                "duration": 900,
                "settle": 0,
                "evaluate": "on_event",
                "events": [{"event_type": "button", "data": {"id": "hall"}, "label": "pressed"}],
                "with_attributes": True,
                "collapse_repeats": False,
            }
        )
    )
    assert window.events == (EventFilter("button", {"id": "hall"}, "pressed"),)
    assert (window.settle, window.evaluate) == (0.0, "on_event")
    assert window.with_attributes
    assert not window.collapse_repeats


# --- YAML ---


async def test_watch_from_yaml_slugs_the_keys(hass: HomeAssistant) -> None:
    raw = WATCH_SCHEMA(
        {
            "name": "Kitchen Watch",
            "note": "{{ 'after dinner' }}",
            "entities": [DISHWASHER],
            "wake_on": [BACK_DOOR],
            "fire_event": False,
            "window": {"duration": 1200},
            "questions": [{"name": "Dishes Done", "type": "noul", "instructions": "x"}],
        }
    )
    watch = watch_from_yaml(hass, raw)
    assert watch.key == "kitchen_watch"
    assert watch.name == "Kitchen Watch"
    assert [q.key for q in watch.questions] == ["kitchen_watch_dishes_done"]
    assert watch.scan_interval == 300
    assert watch.note is not None
    assert watch.note.async_render() == "after dinner"
    assert watch.target == {"entity_id": [DISHWASHER]}
    assert watch.wake_on == (BACK_DOOR,)
    assert watch.window is not None
    assert watch.window.duration == timedelta(minutes=20)
    assert watch.fire_event is False


async def test_watch_from_yaml_without_note_or_window(hass: HomeAssistant) -> None:
    raw = WATCH_SCHEMA(
        {
            "name": "Hall",
            "entities": [BACK_DOOR],
            "scan_interval": 45,
            "questions": [{"name": "Open", "type": "noul", "instructions": "x"}],
        }
    )
    watch = watch_from_yaml(hass, raw)
    assert (watch.note, watch.window, watch.wake_on) == (None, None, ())
    assert watch.scan_interval == 45
    assert watch.fire_event


# --- UI questions grouped into watches ---


def ui_question(title: str, kind: str = "noul", **fields: Any) -> ConfigSubentryData:
    """A question subentry the way the editor stores one."""
    data = {
        "name": title,
        "type": kind,
        "instructions": f"Is the {title.lower()} so?",
        "target": {"entity_id": [DISHWASHER]},
        "scan_interval": 600,
        "with_attributes": False,
        "window_enabled": False,
        **fields,
    }
    return ConfigSubentryData(
        data=data, subentry_type=SUBENTRY_QUESTION, title=title, unique_id=None
    )


def ui_entry(*questions: ConfigSubentryData) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN, title="kitchen", data=ENTRY_DATA["typesafe"], subentries_data=list(questions)
    )


async def test_a_question_saved_with_a_wildcard_event_still_loads_and_ignores_answers(
    hass: HomeAssistant, fake_client: MagicMock
) -> None:
    """The editor refuses `*` now, but a question saved before that is kept."""
    entry = ui_entry(
        ui_question(
            "Rinsed",
            window_enabled=True,
            window_duration={"hours": 0, "minutes": 45, "seconds": 0},
            window_max_events=60,
            window_settle=0,
            window_evaluate="on_event",
            window_collapse_repeats=False,
            window_events=[{"event_type": "*", "data": {}}],
        )
    )
    hass.states.async_set(DISHWASHER, "40")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    asked = fake_client.ask.await_count
    # An answer fires mentat_decision and moves Mentat's own sensors. Heard by
    # `*`, either would settle the window and ask again, forever.
    hass.bus.async_fire("mentat_decision", {"watch": "rinsed"})
    await hass.async_block_till_done()
    assert fake_client.ask.await_count == asked
    hass.bus.async_fire("dishwasher_door", {"open": True})
    await hass.async_block_till_done()
    assert fake_client.ask.await_count == asked + 1


async def test_watches_from_subentries_same_state_is_one_request(
    hass: HomeAssistant, fake_client: MagicMock
) -> None:
    entry = ui_entry(
        ui_question("Rinsed"),
        ui_question("Dried", target={"entity_id": [DISHWASHER], "device_id": []}),
        ui_question("Stage", kind="choice", options="rinse\ndry"),
    )
    hass.states.async_set(DISHWASHER, "40")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    asked = [c.args[1] for c in fake_client.ask.await_args_list if "probe" not in c.args[1]]
    assert len(asked) == 1
    assert len(asked[0]) == 3


async def test_watches_from_subentries_order_of_ids_does_not_split(hass: HomeAssistant) -> None:
    entry = ui_entry(
        ui_question("Rinsed", target={"entity_id": [DISHWASHER, BACK_DOOR]}),
        ui_question("Dried", target={"entity_id": [BACK_DOOR, DISHWASHER]}),
    )
    [watch] = watches_from_subentries(hass, entry)
    assert watch.target == {"entity_id": sorted([DISHWASHER, BACK_DOOR])}


@pytest.mark.parametrize(
    "change",
    [
        {"target": {"entity_id": [BACK_DOOR]}},
        {"scan_interval": 120},
        {"note": "Guests tonight"},
        {"with_attributes": True},
        {"wake_on": [BACK_DOOR]},
        {"window_enabled": True, "window_duration": {"minutes": 15}},
    ],
)
async def test_watches_from_subentries_a_different_state_is_its_own_request(
    hass: HomeAssistant, change: dict[str, Any]
) -> None:
    entry = ui_entry(ui_question("Rinsed"), ui_question("Dried", **change))
    watches = watches_from_subentries(hass, entry)
    assert sorted(w.name for w in watches) == ["Dried", "Rinsed"]
    assert all(len(w.questions) == 1 for w in watches)


async def test_watches_from_subentries_ignore_settings_of_a_window_left_off(
    hass: HomeAssistant,
) -> None:
    entry = ui_entry(ui_question("Rinsed"), ui_question("Dried", window_duration={"minutes": 3}))
    [watch] = watches_from_subentries(hass, entry)
    assert watch.name == "Group 1"
    assert watch.key.startswith("ui_")
    assert len(watch.key) == len("ui_") + 12
    assert watch.window is None


async def test_watches_from_subentries_first_question_sets_the_schedule(
    hass: HomeAssistant,
) -> None:
    entry = ui_entry(
        ui_question("Rinsed", note="{{ 'late' }}", wake_on=[BACK_DOOR]),
        ui_question("Dried", note="{{ 'late' }}", wake_on=[BACK_DOOR]),
    )
    [watch] = watches_from_subentries(hass, entry)
    assert watch.scan_interval == 600
    assert watch.wake_on == (BACK_DOOR,)
    assert watch.note is not None
    assert watch.note.async_render() == "late"
    assert watch.fire_event


async def test_watches_from_subentries_key_questions_by_subentry_id(
    hass: HomeAssistant,
) -> None:
    entry = ui_entry(ui_question("Clean", high="Spotless", low="", brief="Eco mode"))
    [subentry] = entry.subentries.values()
    [watch] = watches_from_subentries(hass, entry)
    [spec] = watch.questions
    assert spec.key == f"ui_{subentry.subentry_id.lower()}"
    assert isinstance(spec.question, Noul)
    assert (spec.question.true, spec.question.false) == ("Spotless", None)
    assert spec.question.instructions == {
        "question": "Is the clean so?",
        "background": "Eco mode",
    }


async def test_watches_from_subentries_window_from_the_editor(hass: HomeAssistant) -> None:
    entry = ui_entry(
        ui_question(
            "Rinsed",
            with_attributes=True,
            window_enabled=True,
            window_duration={"hours": 1, "minutes": 5, "seconds": 0},
            window_max_events=40,
            window_settle=7,
            window_evaluate="interval",
            window_events=[{"event_type": "button", "data": {"id": "hall"}}],
            window_collapse_repeats=False,
        )
    )
    [watch] = watches_from_subentries(hass, entry)
    assert watch.window == WindowConfig(
        duration=timedelta(hours=1, minutes=5),
        max_events=40,
        settle=7.0,
        evaluate="interval",
        events=(EventFilter("button", {"id": "hall"}, None),),
        with_attributes=True,
        collapse_repeats=False,
    )


async def test_watches_from_subentries_skip_other_subentry_types(hass: HomeAssistant) -> None:
    other = ConfigSubentryData(data={}, subentry_type="other", title="x", unique_id=None)
    assert watches_from_subentries(hass, ui_entry(other)) == []


def test_share_key_ignores_empty_target_kinds_and_id_order() -> None:
    plain = {"target": {"entity_id": [DISHWASHER, BACK_DOOR]}}
    noisy = {"target": {"area_id": [], "entity_id": [BACK_DOOR, DISHWASHER]}}
    assert share_key(plain) == share_key(noisy)


def test_share_key_reads_a_missing_schedule_as_the_default() -> None:
    assert share_key({}) == share_key({"scan_interval": 300.0})
    assert share_key({}) != share_key({"scan_interval": 301})
