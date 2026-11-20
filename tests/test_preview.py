"""The editor's preview: the state a draft reads, its trial answer, and the sentences in between."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant, State
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from jevclient import (
    Answer,
    BoxAnswer,
    ChoiceAnswer,
    JevConnectionError,
    JevResponse,
    NoulAnswer,
    ScoreAnswer,
    Usage,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mentat.const import CONF_TOKEN_BUDGET, DOMAIN, SUBENTRY_QUESTION
from custom_components.mentat.preview import (
    BAR_WIDTH,
    Rules,
    describe_window,
    draw_answer,
    fenced,
    readable,
    sentences,
    trial,
)

from .conftest import ENTRY_DATA, FakeRecorder

FREEZER = "sensor.freezer_temperature"
LID = "binary_sensor.chest_freezer_lid"
WINDOW = {
    "window_enabled": True,
    "window_duration": {"hours": 0, "minutes": 20, "seconds": 0},
    "window_max_events": 40,
    "window_settle": 3,
    "window_evaluate": "both",
    "window_collapse_repeats": True,
}


def answered(**answers: Answer) -> JevResponse:
    return JevResponse(
        model="jev-1.15.2",
        answers=answers,
        usage=Usage(input_tokens=187, output_tokens=2),
        latency_ms=44.0,
    )


def saved(title: str) -> ConfigSubentryData:
    return ConfigSubentryData(
        data={
            "name": title,
            "type": "noul",
            "instructions": "Worry?",
            "target": {"entity_id": [FREEZER]},
            "scan_interval": 600,
            "with_attributes": False,
            "window_enabled": False,
        },
        subentry_type=SUBENTRY_QUESTION,
        title=title,
        unique_id=None,
    )


async def utility_room(
    hass: HomeAssistant, *questions: ConfigSubentryData, **options: Any
) -> MockConfigEntry:
    hass.states.async_set(FREEZER, "-19", {"friendly_name": "Freezer"})
    hass.states.async_set(LID, "off", {"friendly_name": "Freezer lid"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="utility",
        data=ENTRY_DATA["typesafe"],
        options=options,
        subentries_data=list(questions),
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def preview_of(hass: HomeAssistant, entry: MockConfigEntry, **fields: Any) -> Any:
    """Open a noul form, fill it in and return the preview step."""
    menu = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_QUESTION), context={"source": "user"}
    )
    form = await hass.config_entries.subentries.async_configure(
        menu["flow_id"], {"next_step_id": "noul"}
    )
    fields = {
        "name": "Thaw",
        "instructions": "Is it thawing?",
        "advanced": {"scan_interval": 600, "with_attributes": False},
        "window": {},
    } | fields
    shown = await hass.config_entries.subentries.async_configure(form["flow_id"], fields)
    assert shown["step_id"] == "preview"
    return shown


async def test_preview_of_a_window_without_a_recorder_says_it_starts_empty(
    hass: HomeAssistant, fake_client: MagicMock
) -> None:
    entry = await utility_room(hass)
    fake_client.ask.return_value = answered(preview=NoulAnswer(noul=0.6))
    shown = await preview_of(hass, entry, target={"entity_id": [FREEZER]}, window=WINDOW)
    placeholders = shown["description_placeholders"]
    assert '"window"' in placeholders["state"]
    assert '"changes_in_window": 0' in placeholders["state"]
    assert placeholders["grouping"] == (
        "Sent as a request of its own.\n\nThe window covers the last 20 minutes. "
        "The recorder is not loaded, so it starts empty and fills as things change."
    )
    assert fake_client.ask.await_args.args[0]["window"]["events"] == 0


async def test_preview_of_a_window_reads_the_wake_on_entities_from_the_recorder(
    hass: HomeAssistant, fake_client: MagicMock, mock_recorder: FakeRecorder
) -> None:
    entry = await utility_room(hass)
    now = dt_util.utcnow()
    mock_recorder.history = {
        LID: [
            State(LID, "on", last_changed=now - timedelta(minutes=14)),
            State(LID, "off", last_changed=now - timedelta(minutes=13)),
            State(LID, "on", last_changed=now - timedelta(minutes=6)),
        ]
    }
    fake_client.ask.return_value = answered(preview=NoulAnswer(noul=0.6))
    shown = await preview_of(
        hass,
        entry,
        target={"entity_id": [FREEZER]},
        advanced={"scan_interval": 600, "with_attributes": False, "wake_on": [LID]},
        window=WINDOW,
    )
    placeholders = shown["description_placeholders"]
    assert [query["entity_ids"] for query in mock_recorder.calls] == [[LID]]
    assert placeholders["grouping"].endswith(
        "The window covers the last 20 minutes. The recorder has 3 changes in it so "
        "far; bus events are logged from the moment the question is saved."
    )
    assert '"to": "off"' in placeholders["state"]


async def test_preview_of_a_broken_template_shows_why_and_asks_nothing(
    hass: HomeAssistant, fake_client: MagicMock
) -> None:
    entry = await utility_room(hass)
    shown = await preview_of(hass, entry, note="{{ 9 // 0 }} C")
    placeholders = shown["description_placeholders"]
    assert placeholders["state"].startswith("```\nThe note's template has an error:")
    assert placeholders["answer"] == "No trial yet. Pick entities or write a note first."
    assert fake_client.ask.await_count == 1


async def test_preview_of_a_target_that_is_gone_shows_the_sentence(
    hass: HomeAssistant, fake_client: MagicMock
) -> None:
    entry = await utility_room(hass)
    shown = await preview_of(hass, entry, target={"area_id": ["demolished_shed"]})
    assert shown["description_placeholders"]["state"].startswith(
        "```\nNothing by these names exists:"
    )
    assert fake_client.ask.await_count == 1


async def test_preview_of_a_long_state_counts_what_it_cut(
    hass: HomeAssistant, fake_client: MagicMock
) -> None:
    entry = await utility_room(hass)
    fake_client.ask.return_value = answered(preview=NoulAnswer(noul=0.6))
    with patch("custom_components.mentat.preview.PREVIEW_CHARS", 12):
        shown = await preview_of(hass, entry, note="frost-" * 7)
    assert shown["description_placeholders"]["state"] == (
        "```\nfrost-frost-\n... 30 more characters\n```"
    )


@pytest.mark.parametrize(
    ("client", "sentence"),
    [
        (
            {"side_effect": JevConnectionError("no route to host")},
            "Trial request failed: TypeSafe Jev did not answer: no route to host",
        ),
        (
            {"return_value": answered(elsewhere=NoulAnswer(noul=0.6))},
            "The reply does not match the question that was sent.",
        ),
    ],
    ids=["unreachable", "other-key"],
)
async def test_trial_that_goes_wrong_says_so_in_a_sentence(
    hass: HomeAssistant, fake_client: MagicMock, client: dict[str, Any], sentence: str
) -> None:
    entry = await utility_room(hass)
    fake_client.ask.side_effect = client.get("side_effect")
    if "return_value" in client:
        fake_client.ask.return_value = client["return_value"]
    shown = await preview_of(hass, entry, note="lid shut")
    assert shown["description_placeholders"]["answer"] == sentence


async def test_trial_over_the_budget_is_not_sent_and_the_question_still_saves(
    hass: HomeAssistant, fake_client: MagicMock
) -> None:
    entry = await utility_room(hass, **{CONF_TOKEN_BUDGET: 90})
    shown = await preview_of(hass, entry, note="lid shut")
    assert shown["description_placeholders"]["answer"].startswith(
        "Today's budget cannot cover a trial"
    )
    assert fake_client.ask.await_count == 1
    done = await hass.config_entries.subentries.async_configure(shown["flow_id"], {})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


async def test_trial_with_a_cleared_cutoff_draws_the_bar_alone(
    hass: HomeAssistant, fake_client: MagicMock
) -> None:
    """The editor keeps a cleared number box as "", which is no cutoff at all."""
    entry = await utility_room(hass)
    fake_client.ask.return_value = answered(preview=NoulAnswer(noul=0.25))
    draft = {"name": "Thaw", "type": "noul", "instructions": "Is it thawing?", "cutoff": ""}
    drawn = await trial(hass, entry, draft, "lid shut")
    assert drawn.startswith(f"`{'█' * round(0.25 * BAR_WIDTH)}")
    assert "verdict" not in drawn
    assert "187 tokens" in drawn


async def test_sharing_lists_the_saved_questions_that_read_the_same_thing(
    hass: HomeAssistant, fake_client: MagicMock
) -> None:
    entry = await utility_room(hass, saved("Ice build-up"), saved("Door seal"))
    fake_client.ask.return_value = answered(preview=NoulAnswer(noul=0.25))
    shown = await preview_of(hass, entry, target={"entity_id": [FREEZER]}, cutoff=0.3)
    placeholders = shown["description_placeholders"]
    assert placeholders["grouping"] == ("Shares a request with Door seal, Ice build-up.")
    assert "With a cutoff of 0.3, the verdict sensor would read off." in placeholders["answer"]


# --- the pieces on their own ---


async def test_draw_answer_draws_a_noul_as_one_bar(hass: HomeAssistant) -> None:
    say = await sentences(hass)
    drawn = draw_answer(NoulAnswer(noul=0.5), None, say)
    assert drawn == f"`{'█' * (BAR_WIDTH // 2)}{'░' * (BAR_WIDTH // 2)}`  **0.50**"


@pytest.mark.parametrize(
    ("noul", "cutoffs", "sentence"),
    [
        (0.7, Rules(0.7, 0.3), "With a cutoff of 0.7, the verdict sensor would read on."),
        (0.5, Rules(0.7), "With a cutoff of 0.7, the verdict sensor would read off."),
        (0.2, Rules(0.7, 0.3), "Under the release of 0.3, the verdict sensor would read off."),
        (
            0.3,
            Rules(0.7, 0.3),
            "Between the release of 0.3 and the cutoff of 0.7, "
            "the verdict sensor would keep the state it had.",
        ),
    ],
    ids=["over", "under-no-release", "under-release", "held"],
)
async def test_draw_answer_says_where_a_noul_leaves_its_verdict(
    hass: HomeAssistant, noul: float, cutoffs: Rules, sentence: str
) -> None:
    say = await sentences(hass)
    assert draw_answer(NoulAnswer(noul=noul), cutoffs, say).split("\n\n")[1] == sentence


@pytest.mark.parametrize(
    ("rules", "sentence"),
    [
        (Rules(min_confidence=0.9, fallback="unsure"), "would read unsure."),
        (Rules(min_confidence=0.9), "would read unknown."),
    ],
    ids=["fallback", "unknown"],
)
async def test_draw_answer_says_what_the_sensor_shows_under_its_floor(
    hass: HomeAssistant, rules: Rules, sentence: str
) -> None:
    say = await sentences(hass)
    choice = ChoiceAnswer(choice="b", probabilities={"a": 0.3, "b": 0.7}, confidence=0.4)
    drawn = draw_answer(choice, rules, say).split("\n\n")
    assert drawn[-1] == f"Under the lowest confidence of 0.9, the sensor {sentence}"


async def test_draw_answer_of_a_confident_choice_adds_no_sentence(hass: HomeAssistant) -> None:
    say = await sentences(hass)
    choice = ChoiceAnswer(choice="b", probabilities={"a": 0.1, "b": 0.9}, confidence=0.8)
    assert len(draw_answer(choice, Rules(min_confidence=0.8), say).split("\n\n")) == 2


async def test_draw_answer_ranks_a_choice_and_keeps_a_score_in_level_order(
    hass: HomeAssistant,
) -> None:
    say = await sentences(hass)
    choice = ChoiceAnswer(choice="b", probabilities={"a": 0.0, "b": 1.0}, confidence=0.9)
    rows = draw_answer(choice, None, say).split("\n\n")[0].splitlines()
    assert rows == [f"`{'█' * BAR_WIDTH}` 1.00  **b**", f"`{'░' * BAR_WIDTH}` 0.00  a"]
    score = ScoreAnswer(
        score=1.0,
        legend={"0": "Low", "1": "High"},
        probabilities={"1": 0.75, "0": 0.25},
        confidence=0.5,
    )
    rows = draw_answer(score, None, say).split("\n\n")[0].splitlines()
    assert [row.rsplit("  ", 1)[1] for row in rows] == ["Low", "High"]


async def test_draw_answer_of_a_box_says_it_has_no_drawing(hass: HomeAssistant) -> None:
    say = await sentences(hass)
    box = BoxAnswer(top=0.1, bottom=0.2, left=0.3, right=0.4, confidence=0.5)
    assert (
        draw_answer(box, Rules(0.5), say)
        == "This version of Mentat cannot show an answer of that shape."
    )


async def test_sentences_of_an_unknown_key_returns_the_key(hass: HomeAssistant) -> None:
    say = await sentences(hass)
    assert say("trial_no_such_sentence") == "trial_no_such_sentence"


async def test_readable_of_an_untranslated_error_is_its_own_text(hass: HomeAssistant) -> None:
    assert await readable(hass, HomeAssistantError("the freezer is on fire")) == (
        "the freezer is on fire"
    )


@pytest.mark.parametrize("state", [None, "lid shut", {"entities": []}, {"window": "x"}])
async def test_describe_window_of_a_state_without_one_is_none(
    hass: HomeAssistant, state: Any
) -> None:
    assert await describe_window(hass, state) is None


@pytest.mark.parametrize(
    ("text", "fence"),
    [
        ('{"note": "plain"}', "```"),
        ("```\n![x](https://192.0.2.9/p.png)", "````"),
        ("a ````` run", "``````"),
    ],
    ids=["plain", "closing-fence", "long-run"],
)
def test_the_state_is_fenced_past_any_backticks_it_holds(text: str, fence: str) -> None:
    shown = fenced(text)
    assert shown == f"{fence}\n{text}\n{fence}"
