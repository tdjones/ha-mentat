"""The question editor: its forms, what they refuse, and what a saved question becomes."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from jevclient import (
    Answer,
    Choice,
    ChoiceAnswer,
    JevResponse,
    Noul,
    NoulAnswer,
    Score,
    ScoreAnswer,
    Usage,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mentat.const import DOMAIN, SUBENTRY_QUESTION
from custom_components.mentat.editor import FORMS, flatten, nest, option_problem, validate
from custom_components.mentat.watches import EventFilter, watches_from_subentries

from .conftest import ENTRY_DATA, PROBE_TOKENS

FRIDGE = "sensor.fridge_door_contact"
FREEZER = "sensor.freezer_temperature"
TRIAL_TOKENS = 233
SCHEDULE = {"scan_interval": 900, "with_attributes": False}
WINDOW = {
    "window_enabled": True,
    "window_duration": {"hours": 0, "minutes": 45, "seconds": 0},
    "window_max_events": 60,
    "window_settle": 5,
    "window_evaluate": "on_event",
    "window_collapse_repeats": False,
}


def answered(**answers: Answer) -> JevResponse:
    return JevResponse(
        model="jev-1.15.2",
        answers=answers,
        usage=Usage(input_tokens=TRIAL_TOKENS, output_tokens=2),
        latency_ms=61.0,
    )


def saved(title: str, kind: str = "noul", **extra: Any) -> ConfigSubentryData:
    """A question as the editor stores one."""
    return ConfigSubentryData(
        data={
            "name": title,
            "type": kind,
            "instructions": f"Is the {title.lower()} a worry?",
            "target": {"entity_id": [FREEZER]},
            "scan_interval": 900,
            "with_attributes": False,
            "window_enabled": False,
            **extra,
        },
        subentry_type=SUBENTRY_QUESTION,
        title=title,
        unique_id=None,
    )


async def kitchen(hass: HomeAssistant, *questions: ConfigSubentryData) -> MockConfigEntry:
    hass.states.async_set(FREEZER, "-17", {"friendly_name": "Freezer"})
    hass.states.async_set(FRIDGE, "closed", {"friendly_name": "Fridge door"})
    entry = MockConfigEntry(
        domain=DOMAIN, title="kitchen", data=ENTRY_DATA["typesafe"], subentries_data=list(questions)
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def open_form(hass: HomeAssistant, entry: MockConfigEntry, kind: str) -> str:
    menu = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_QUESTION), context={"source": "user"}
    )
    assert menu["type"] is FlowResultType.MENU
    assert menu["menu_options"] == ["noul", "choice", "score"]
    form = await hass.config_entries.subentries.async_configure(
        menu["flow_id"], {"next_step_id": kind}
    )
    assert (form["type"], form["step_id"]) == (FlowResultType.FORM, kind)
    return str(form["flow_id"])


async def open_saved(hass: HomeAssistant, entry: MockConfigEntry, subentry_id: str) -> str:
    form = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_QUESTION),
        context={"source": "reconfigure", "subentry_id": subentry_id},
    )
    assert (form["type"], form["step_id"]) == (FlowResultType.FORM, "reconfigure")
    return str(form["flow_id"])


async def fill(hass: HomeAssistant, flow_id: str, **fields: Any) -> Any:
    fields.setdefault("advanced", SCHEDULE)
    fields.setdefault("window", {})
    return await hass.config_entries.subentries.async_configure(flow_id, fields)


async def confirm(hass: HomeAssistant, flow_id: str) -> Any:
    return await hass.config_entries.subentries.async_configure(flow_id, {})


# --- the text boxes and the sections ---


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("open\nshut\n\n", None),
        ("open: the seal is broken\n:  ajar", "option_unnamed"),
        ("open\nshut: closed\nopen: wide", "option_repeated"),
    ],
)
def test_option_problem_finds_a_line_that_would_drop_an_option(
    text: str, problem: str | None
) -> None:
    assert option_problem(text) == problem


def test_flatten_lifts_both_sections_and_nest_puts_them_back() -> None:
    form = {"name": "Fridge", "advanced": {"scan_interval": 120}, "window": WINDOW}
    flat = flatten(form)
    assert flat["scan_interval"] == 120
    assert flat["window_evaluate"] == "on_event"
    assert "advanced" not in flat
    assert nest(flat) == form


def test_forms_carry_only_their_own_answer_fields() -> None:
    keys = {kind: {str(marker) for marker in form.schema} for kind, form in FORMS.items()}
    assert {"high", "low", "cutoff"} <= keys["noul"]
    assert "options" in keys["choice"]
    assert "scale" in keys["score"]
    assert not {"options", "scale"} & keys["noul"]
    assert all({"advanced", "window"} <= shown for shown in keys.values())


def test_validate_keeps_the_first_error_on_each_field() -> None:
    data = {"options": "a\na", "window_enabled": True, "window_duration": {"seconds": 5}}
    assert validate("choice", data) == {
        "base": "window_without_target",
        "options": "option_repeated",
    }


# --- adding a question ---


async def test_noul_form_previews_the_trial_and_then_saves_both_sensors(
    hass: HomeAssistant, fake_client: MagicMock
) -> None:
    entry = await kitchen(hass)
    flow_id = await open_form(hass, entry, "noul")
    fake_client.ask.return_value = answered(preview=NoulAnswer(noul=0.42))
    preview = await fill(
        hass,
        flow_id,
        name="Freezer thawing",
        instructions="Is the freezer warming up?",
        target={"entity_id": [FREEZER]},
        high="Warming and will thaw food",
        cutoff=0.35,
    )
    assert (preview["type"], preview["step_id"]) == (FlowResultType.FORM, "preview")
    shown = preview["description_placeholders"]
    assert FREEZER in shown["state"]
    assert shown["grouping"] == "Sent as a request of its own."
    assert "**0.42**" in shown["answer"]
    assert "With a cutoff of 0.35, the verdict sensor would read on." in shown["answer"]
    assert f"This trial cost {TRIAL_TOKENS} tokens" in shown["answer"]
    assert f"{PROBE_TOKENS} input tokens" not in shown["answer"]
    assert entry.runtime_data.ledger.requests == 2
    asked = fake_client.ask.await_args.args[1]["preview"]
    assert isinstance(asked, Noul)
    assert asked.true == "Warming and will thaw food"

    done = await confirm(hass, flow_id)
    assert (done["type"], done["title"]) == (FlowResultType.CREATE_ENTRY, "Freezer thawing")
    await hass.async_block_till_done()
    [question] = entry.subentries.values()
    assert question.data["cutoff"] == 0.35
    assert question.data["scan_interval"] == 900
    assert question.data["window_enabled"] is False
    assert "advanced" not in question.data
    unique = f"{entry.entry_id}:ui_{question.subentry_id.lower()}"
    registry = er.async_get(hass)
    assert (
        registry.async_get_entity_id("sensor", DOMAIN, unique) == "sensor.kitchen_freezer_thawing"
    )
    assert registry.async_get_entity_id("binary_sensor", DOMAIN, f"{unique}:verdict")


async def test_choice_form_sends_its_lines_as_criteria_and_draws_the_winner_first(
    hass: HomeAssistant, fake_client: MagicMock
) -> None:
    entry = await kitchen(hass)
    flow_id = await open_form(hass, entry, "choice")
    fake_client.ask.return_value = answered(
        preview=ChoiceAnswer(
            choice="milk", probabilities={"butter": 0.15, "milk": 0.85}, confidence=0.62
        )
    )
    preview = await fill(
        hass,
        flow_id,
        name="Running low",
        instructions="What runs out first?",
        note="Half a pint left, butter untouched",
        options="butter: the yellow block\nmilk",
    )
    shown = preview["description_placeholders"]
    assert "The model leans to **milk**. Confidence in this answer: 0.62." in shown["answer"]
    assert shown["answer"].index("**milk**") < shown["answer"].index("butter")
    assert shown["state"] == "Half a pint left, butter untouched"
    asked = fake_client.ask.await_args.args[1]["preview"]
    assert isinstance(asked, Choice)
    assert asked.criteria == {"butter": "the yellow block", "milk": None}


async def test_score_form_sends_its_levels_in_order_and_folds_the_brief(
    hass: HomeAssistant, fake_client: MagicMock
) -> None:
    entry = await kitchen(hass)
    flow_id = await open_form(hass, entry, "score")
    fake_client.ask.return_value = answered(
        preview=ScoreAnswer(
            score=0.7,
            legend={"0": "Fine", "1": "Check it", "2": "Bin it"},
            probabilities={"0": 0.35, "1": 0.6, "2": 0.05},
            confidence=0.48,
        )
    )
    preview = await fill(
        hass,
        flow_id,
        name="Food safety",
        instructions="Is the food still safe?",
        target={"entity_id": [FREEZER]},
        brief="Above -12 C for two hours means bin it.",
        scale="Fine\nCheck it\nBin it",
    )
    shown = preview["description_placeholders"]
    assert (
        "lands at **0.70**, nearest to **Check it**. Confidence in this answer: 0.48."
        in (shown["answer"])
    )
    asked = fake_client.ask.await_args.args[1]["preview"]
    assert isinstance(asked, Score)
    assert list(asked.criteria) == ["Fine", "Check it", "Bin it"]
    assert asked.instructions == {
        "question": "Is the food still safe?",
        "background": "Above -12 C for two hours means bin it.",
    }


@pytest.mark.parametrize(
    ("kind", "fields", "errors"),
    [
        ("noul", {}, {"base": "nothing_to_read"}),
        ("noul", {"target": {"entity_id": [], "area_id": []}}, {"base": "nothing_to_read"}),
        ("choice", {"note": "n", "options": "milk\n: cream"}, {"options": "option_unnamed"}),
        ("choice", {"note": "n", "options": "milk\nmilk: again"}, {"options": "option_repeated"}),
        ("choice", {"note": "n", "options": "milk"}, {"options": "options_count"}),
        ("score", {"note": "n", "scale": "Fine\n\n"}, {"scale": "scale_count"}),
        ("noul", {"note": "n", "window": WINDOW}, {"base": "window_without_target"}),
        (
            "noul",
            {
                "target": {"entity_id": [FREEZER]},
                "window": WINDOW | {"window_duration": {"seconds": 45}},
            },
            {"base": "window_duration_out_of_range"},
        ),
        (
            "noul",
            {
                "target": {"entity_id": [FREEZER]},
                "window": WINDOW | {"window_duration": {"minutes": 2}, "window_settle": 150},
            },
            {"base": "window_settle_too_long"},
        ),
        (
            "noul",
            {
                "target": {"entity_id": [FREEZER]},
                "window": WINDOW | {"window_events": [{"label": "no type"}]},
            },
            {"base": "window_events_invalid"},
        ),
        (
            "noul",
            {
                "target": {"entity_id": [FREEZER]},
                "window": WINDOW
                | {
                    "window_events": [
                        {"event_type": "zha_event"},
                        {"event_type": "mentat_decision"},
                    ]
                },
            },
            {"base": "window_event_is_own"},
        ),
    ],
    ids=[
        "empty",
        "emptied-picker",
        "unnamed",
        "repeated",
        "one-option",
        "one-level",
        "window-no-target",
        "window-short",
        "settle-long",
        "events-bad",
        "events-own",
    ],
)
async def test_form_with_a_problem_shows_again_with_the_error_on_its_field(
    hass: HomeAssistant,
    fake_client: MagicMock,
    kind: str,
    fields: dict[str, Any],
    errors: dict[str, str],
) -> None:
    entry = await kitchen(hass)
    flow_id = await open_form(hass, entry, kind)
    again = await fill(hass, flow_id, name="Q", instructions="Worry?", **fields)
    assert (again["type"], again["step_id"]) == (FlowResultType.FORM, kind)
    assert again["errors"] == errors
    assert fake_client.ask.await_count == 1


async def test_window_question_saves_into_a_watch_with_its_window(
    hass: HomeAssistant, fake_client: MagicMock
) -> None:
    entry = await kitchen(hass)
    flow_id = await open_form(hass, entry, "noul")
    fake_client.ask.return_value = answered(preview=NoulAnswer(noul=0.05))
    events = [{"event_type": "zha_event", "data": {"command": "open"}, "label": "door"}]
    preview = await fill(
        hass,
        flow_id,
        name="Door left open",
        instructions="Was the fridge left open?",
        target={"entity_id": [FRIDGE]},
        window=WINDOW | {"window_events": events},
    )
    assert preview["step_id"] == "preview"
    assert (await confirm(hass, flow_id))["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    [watch] = watches_from_subentries(hass, entry)
    assert watch.window is not None
    assert watch.window.duration == timedelta(minutes=45)
    assert watch.window.events == (
        EventFilter(event_type="zha_event", data={"command": "open"}, label="door"),
    )


# --- changing a saved question ---


async def test_reconfigure_saves_in_place_and_does_not_share_with_itself(
    hass: HomeAssistant, fake_client: MagicMock
) -> None:
    entry = await kitchen(hass, saved("Shelf", kind="choice", options="milk\nbutter"), saved("Ice"))
    [shelf] = [sub.subentry_id for sub in entry.subentries.values() if sub.title == "Shelf"]
    flow_id = await open_saved(hass, entry, shelf)
    fake_client.ask.return_value = answered(
        preview=ChoiceAnswer(
            choice="milk", probabilities={"milk": 0.5, "butter": 0.3, "jam": 0.2}, confidence=0.2
        )
    )
    preview = await fill(
        hass,
        flow_id,
        name="Top shelf",
        instructions="What is on the top shelf?",
        target={"entity_id": [FREEZER]},
        options="milk\nbutter\njam",
    )
    assert preview["step_id"] == "preview"
    assert preview["description_placeholders"]["grouping"] == ("Shares a request with Ice.")
    done = await confirm(hass, flow_id)
    assert (done["type"], done["reason"]) == (FlowResultType.ABORT, "reconfigure_successful")
    await hass.async_block_till_done()
    edited = entry.subentries[shelf]
    assert edited.title == "Top shelf"
    assert edited.data["options"] == "milk\nbutter\njam"
    assert edited.data["type"] == "choice"


async def test_reconfigure_with_a_problem_shows_its_own_step_again(
    hass: HomeAssistant, fake_client: MagicMock
) -> None:
    entry = await kitchen(hass, saved("Safety", kind="score", scale="Fine\nBin it"))
    [safety] = list(entry.subentries)
    flow_id = await open_saved(hass, entry, safety)
    again = await fill(hass, flow_id, name="Safety", instructions="Safe?", note="n", scale="Fine")
    assert again["step_id"] == "reconfigure"
    assert again["errors"] == {"scale": "scale_count"}
