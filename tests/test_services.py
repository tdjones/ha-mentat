"""The actions: what each one sends, what it answers and what it refuses unsent."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import Context, HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from jevclient import (
    Answer,
    Box,
    BoxAnswer,
    ChoiceAnswer,
    Image,
    JevAuthError,
    JevConnectionError,
    JevRateLimitError,
    JevResponse,
    JevValidationError,
    NoulAnswer,
    ScoreAnswer,
    Usage,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser

from custom_components.mentat.const import CONF_TOKEN_BUDGET, DOMAIN
from custom_components.mentat.pictures import Picture, Pictures
from custom_components.mentat.providers import Dialect
from custom_components.mentat.services import async_register_services

from .conftest import ENTRY_DATA, PROBE_TOKENS, WASHER, FakeRecorder

FETCH = "custom_components.mentat.services.async_request_images"
MODEL = "jev-1.15.2"
BILLED = 512
DRYER = "sensor.dryer_drum_temperature"
# A call with something to read and a question, and nothing else.
PLAIN = {"note": "The dryer drum has been at 31 C for twenty minutes.", "instructions": "Cool?"}

PANTRY = ChoiceAnswer(choice="flour", probabilities={"flour": 0.62, "sugar": 0.38}, confidence=0.57)
LEAK = ScoreAnswer(
    score=1.2,
    legend={"0": "Dry", "1": "Damp", "2": "Dripping", "3": "Flooding"},
    probabilities={"0": 0.05, "1": 0.7, "2": 0.2, "3": 0.05},
    confidence=0.66,
)


def reply(*, model: str = MODEL, **answers: Answer) -> JevResponse:
    return JevResponse(
        model=model,
        answers=answers,
        usage=Usage(input_tokens=BILLED, output_tokens=7),
        latency_ms=118.46,
    )


async def act(
    hass: HomeAssistant, action: str, data: dict[str, Any], user_id: str | None = None
) -> Any:
    return await hass.services.async_call(
        DOMAIN,
        action,
        data,
        blocking=True,
        return_response=True,
        context=Context(user_id=user_id),
    )


def sent(client: MagicMock) -> tuple[Any, dict[str, Any]]:
    state, questions = client.ask.await_args.args
    return state, questions


def only_the_probe_was_sent(client: MagicMock) -> bool:
    return client.ask.await_count == 1


def reader_of(hass: HomeAssistant, *entity_ids: str) -> MockUser:
    """A user who is no administrator and may read only these entities."""
    user = MockUser().add_to_hass(hass)
    user.mock_policy({"entities": {"entity_ids": dict.fromkeys(entity_ids, True)}})
    return user


def back_porch() -> Pictures:
    picture = Picture(
        payload=Image(data=b"\xff\xd8not-really", format="jpg"),
        sent=(1280, 960),
        source=(3264, 2448),
    )
    return Pictures(
        dialect=Dialect.TYPESAFE,
        entries=[{"name": "Back porch", "entity_id": "camera.back_porch", "image": "bp"}],
        assets={"bp": picture.payload},
        ordered=[picture],
        tokens=2210,
    )


# --- answers ---


async def test_noul_over_its_cutoff_returns_a_true_verdict_and_bills_the_ledger(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_client: MagicMock
) -> None:
    fake_client.ask.return_value = reply(answer=NoulAnswer(noul=0.64))
    answered = await act(hass, "noul", {**PLAIN, "cutoff": 0.6})
    assert answered == {
        "noul": 0.64,
        "verdict": True,
        "cutoff": 0.6,
        "model": MODEL,
        "latency_ms": 118.5,
        "usage": {"input_tokens": BILLED, "output_tokens": 7},
    }
    runtime = loaded.runtime_data
    assert runtime.ledger.tokens == PROBE_TOKENS + BILLED
    assert runtime.model_version == MODEL


async def test_noul_with_a_blank_high_sends_only_low_and_folds_the_brief(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_client: MagicMock
) -> None:
    fake_client.ask.return_value = reply(answer=NoulAnswer(noul=0.31))
    answered = await act(
        hass, "noul", {**PLAIN, "high": "", "low": "Still hot", "brief": "Cool is under 30 C"}
    )
    assert answered["verdict"] is False
    question = sent(fake_client)[1]["answer"]
    assert (question.true, question.false) == (None, "Still hot")
    assert question.instructions == {"question": "Cool?", "background": "Cool is under 30 C"}


async def test_choice_mapping_sends_criteria_and_answers_the_distribution(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_client: MagicMock
) -> None:
    fake_client.ask.return_value = reply(answer=PANTRY)
    answered = await act(
        hass,
        "choice",
        {
            "note": "The jar on the left is nearly empty.",
            "instructions": "What runs out first?",
            "options": {"flour": "The white powder", "sugar": ""},
        },
    )
    assert answered == {
        "choice": "flour",
        "confidence": 0.57,
        "probabilities": {"flour": 0.62, "sugar": 0.38},
        "model": MODEL,
        "latency_ms": 118.5,
        "usage": {"input_tokens": BILLED, "output_tokens": 7},
    }
    assert sent(fake_client)[1]["answer"].criteria == {"flour": "The white powder", "sugar": None}


async def test_score_answer_is_normalised_by_its_top_level(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_client: MagicMock
) -> None:
    fake_client.ask.return_value = reply(answer=LEAK)
    answered = await act(
        hass,
        "score",
        {
            "note": "a ring of water under the boiler",
            "instructions": "How bad is the leak?",
            "scale": ["Dry", "Damp", "Dripping", "Flooding"],
        },
    )
    assert answered["score"] == 1.2
    assert answered["normalized"] == pytest.approx(0.4)
    assert answered["nearest_level"] == "Damp"
    assert answered["confidence"] == 0.66
    assert answered["legend"] == LEAK.legend
    assert answered["probabilities"] == LEAK.probabilities
    assert "type" not in answered


async def test_ask_answers_each_question_under_the_key_the_caller_gave(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_client: MagicMock
) -> None:
    fake_client.ask.return_value = reply(cool=NoulAnswer(noul=0.12), jar=PANTRY, leak=LEAK)
    answered = await act(
        hass,
        "ask",
        {
            "note": "Utility room at 07:10",
            "questions": {
                "cool": {"type": "noul", "instructions": "Is the dryer cool?"},
                "jar": {
                    "type": "choice",
                    "instructions": "Which jar is lower?",
                    "criteria": {"flour": None, "sugar": None},
                },
                "leak": {
                    "type": "score",
                    "instructions": "How bad?",
                    "criteria": ["Dry", "Damp", "Dripping", "Flooding"],
                },
            },
        },
    )
    assert answered["answers"]["cool"] == {"type": "noul", "noul": 0.12}
    assert answered["answers"]["jar"]["choice"] == "flour"
    assert "verdict" not in answered["answers"]["jar"]
    assert answered["answers"]["leak"]["nearest_level"] == "Damp"
    assert answered["model"] == MODEL
    assert sorted(sent(fake_client)[1]) == ["cool", "jar", "leak"]


async def test_box_answers_edges_in_the_size_the_source_had(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_client: MagicMock
) -> None:
    fake_client.ask.return_value = reply(
        answer=BoxAnswer(top=0.2, bottom=0.45, left=0.6, right=0.85, confidence=0.73)
    )
    pictures = back_porch()
    with patch(FETCH, AsyncMock(return_value=pictures)):
        answered = await act(
            hass, "box", {"picture": "camera.back_porch", "instructions": "the cat", "model": MODEL}
        )
    assert answered == {
        "left": 0.6,
        "top": 0.2,
        "right": 0.85,
        "bottom": 0.45,
        "confidence": 0.73,
        "width": 3264,
        "height": 2448,
        "model": MODEL,
        "latency_ms": 118.5,
        "usage": {"input_tokens": BILLED, "output_tokens": 7},
    }
    state, questions = sent(fake_client)
    assert state == {"images": pictures.entries}
    assert isinstance(questions["answer"], Box)
    assert fake_client.ask.await_args.kwargs == {"model": MODEL, "pictures": pictures}


async def test_box_with_a_one_off_model_leaves_the_entry_model_alone(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_client: MagicMock
) -> None:
    loaded.runtime_data.model_version = "jev-1.15.0"
    fake_client.ask.return_value = reply(
        model="jev-1.16.0-rc1",
        answer=BoxAnswer(top=0.0, bottom=0.5, left=0.0, right=0.5, confidence=0.4),
    )
    with patch(FETCH, AsyncMock(return_value=back_porch())):
        await act(
            hass,
            "box",
            {"picture": "camera.back_porch", "instructions": "the cat", "model": "jev-1.16.0-rc1"},
        )
    assert loaded.runtime_data.model_version == "jev-1.15.0"


@pytest.mark.parametrize(("noul", "verdict"), [(0.7, True), (0.69, False)])
async def test_ask_gives_a_noul_with_a_cutoff_its_verdict(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_client: MagicMock, noul: float, verdict: bool
) -> None:
    fake_client.ask.return_value = reply(cool=NoulAnswer(noul=noul))
    answered = await act(
        hass,
        "ask",
        {
            "note": "Utility room at 07:10",
            "questions": {"cool": {"type": "noul", "instructions": "Cool?", "cutoff": 0.7}},
        },
    )
    assert answered["answers"]["cool"] == {
        "type": "noul",
        "noul": noul,
        "verdict": verdict,
        "cutoff": 0.7,
    }


async def test_ask_with_a_box_fetches_every_source_in_order_and_bills_pixels(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_client: MagicMock
) -> None:
    fake_client.ask.return_value = reply(
        cat=BoxAnswer(top=0.1, bottom=0.9, left=0.1, right=0.9, confidence=0.6)
    )
    sources = ["camera.back_porch", "/media/porch/empty.jpg"]
    with patch(FETCH, AsyncMock(return_value=back_porch())) as fetch:
        answered = await act(
            hass,
            "ask",
            {"pictures": sources, "questions": {"cat": {"type": "box", "instructions": "the cat"}}},
        )
    assert list(fetch.await_args.args[1]) == sources
    assert answered["answers"]["cat"]["type"] == "box"
    assert fake_client.ask.await_args.kwargs["pictures"].tokens == 2210


async def test_noul_on_a_target_sends_its_records_beside_the_note(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_client: MagicMock
) -> None:
    hass.states.async_set(DRYER, "31", {"friendly_name": "Dryer drum"})
    fake_client.ask.return_value = reply(answer=NoulAnswer(noul=0.5))
    await act(hass, "noul", {"entity_id": DRYER, "note": "garage", "instructions": "Cool?"})
    state, _ = sent(fake_client)
    assert state["entities"][0]["name"] == "Dryer drum"
    assert state["note"] == "garage"


WINDOWED = {
    "entity_id": WASHER,
    "questions": {"rinsed": {"type": "noul", "instructions": "Has the rinse finished?"}},
    "window": {"duration": "00:40:00", "max_events": 12},
}


async def test_ask_with_a_window_reads_the_recorder_once_and_stops_listening(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_client: MagicMock,
    mock_recorder: FakeRecorder,
) -> None:
    now = dt_util.utcnow()
    hass.states.async_set(WASHER, "4.5", {"friendly_name": "Washer"})
    mock_recorder.history = {
        WASHER: [
            State(WASHER, "2100", last_changed=now - timedelta(minutes=55)),
            State(WASHER, "180", last_changed=now - timedelta(minutes=15)),
            State(WASHER, "4.5", last_changed=now - timedelta(minutes=2)),
        ]
    }
    fake_client.ask.return_value = reply(rinsed=NoulAnswer(noul=0.83))
    answered = await act(hass, "ask", {**WINDOWED, "note": "garage"})
    assert answered["answers"]["rinsed"]["noul"] == 0.83
    state, _ = sent(fake_client)
    assert state["window"]["events"] == 2
    assert [(row["from"], row["to"]) for row in state["events"]] == [
        ("2100", "180"),
        ("180", "4.5"),
    ]
    assert state["entities"][0]["changes_in_window"] == 2
    assert state["note"] == "garage"
    assert [query["entity_ids"] for query in mock_recorder.calls] == [[WASHER]]
    hass.states.async_set(WASHER, "0")
    await hass.async_block_till_done()
    assert fake_client.ask.await_count == 2


async def test_ask_with_a_window_and_no_recorder_sends_an_empty_log(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_client: MagicMock
) -> None:
    hass.states.async_set(WASHER, "4.5")
    fake_client.ask.return_value = reply(rinsed=NoulAnswer(noul=0.2))
    await act(hass, "ask", WINDOWED)
    state, _ = sent(fake_client)
    assert (state["window"]["events"], state["events"]) == (0, [])


# --- refused before anything is sent ---


@pytest.mark.parametrize(
    ("action", "change", "key"),
    [
        ("choice", {"options": ["flour"]}, "options_count"),
        ("score", {"scale": ["Dry"]}, "scale_count"),
        ("noul", {"note": None}, "nothing_to_read"),
        ("noul", {"note": "{{ 34 / 0 }}"}, "template_failed"),
        ("noul", {"pictures": ["switch.porch_light"]}, "not_an_image_source"),
        ("noul", {"pictures": [f"camera.c{n}" for n in range(5)]}, "too_many_images"),
    ],
    ids=["one-option", "one-level", "nothing", "bad-template", "switch", "five-cameras"],
)
async def test_action_with_a_bad_field_is_refused_unsent(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_client: MagicMock,
    action: str,
    change: dict[str, Any],
    key: str,
) -> None:
    data = {name: value for name, value in (PLAIN | change).items() if value is not None}
    with pytest.raises(HomeAssistantError) as caught:
        await act(hass, action, data)
    assert (caught.value.translation_domain, caught.value.translation_key) == (DOMAIN, key)
    assert only_the_probe_was_sent(fake_client)


@pytest.mark.parametrize(
    ("questions", "extra", "key"),
    [
        ({"a": {"type": "score"}}, {}, "question_shape"),
        ({"a": {"instructions": "Cool?"}}, {}, "question_shape"),
        (
            {"a": {"type": "choice", "instructions": "Which?", "criteria": {"flour": None}}},
            {},
            "question_invalid",
        ),
        ({"a": {"type": "ranking", "instructions": "Order them"}}, {}, "question_invalid"),
        ({"a": {"type": "choice", "instructions": "Which?"}}, {}, "question_invalid"),
        ({"a": {"type": "box", "instructions": "the cat"}}, {}, "box_needs_image"),
        (
            {"a": {"type": "noul", "instructions": "Cool?", "cutoff": 0.7, "release": 0.3}},
            {},
            "question_release",
        ),
        (
            {
                "a": {
                    "type": "choice",
                    "instructions": "Which?",
                    "criteria": {"flour": None, "sugar": None},
                    "cutoff": 0.5,
                }
            },
            {},
            "question_cutoff_noul_only",
        ),
        ({"a": {"type": "noul", "instructions": "Cool?", "cutoff": 1.5}}, {}, "question_invalid"),
        (
            {"a": {"type": "noul", "instructions": "Cool?"}},
            {"window": {"duration": "00:10:00"}},
            "window_without_target",
        ),
    ],
    ids=[
        "no-instructions",
        "no-type",
        "one-criterion",
        "unknown-type",
        "no-options",
        "box",
        "release",
        "cutoff-on-choice",
        "cutoff-range",
        "win",
    ],
)
async def test_ask_with_a_question_it_cannot_send_names_the_key(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_client: MagicMock,
    questions: dict[str, Any],
    extra: dict[str, Any],
    key: str,
) -> None:
    with pytest.raises(ServiceValidationError) as caught:
        await act(hass, "ask", {"note": "garage", "questions": questions, **extra})
    assert caught.value.translation_key == key
    if key.startswith(("question", "box")):
        assert caught.value.translation_placeholders["key"] == "a"
    assert only_the_probe_was_sent(fake_client)


@pytest.mark.parametrize(
    ("action", "data"),
    [("noul", PLAIN | {"high": True}), ("score", PLAIN | {"scale": ["Dry", False]})],
    ids=["high", "scale"],
)
async def test_schema_with_a_bare_yes_or_no_asks_for_quotes(
    hass: HomeAssistant, loaded: MockConfigEntry, action: str, data: dict[str, Any]
) -> None:
    with pytest.raises(vol.Invalid, match="put the word in quotes"):
        await act(hass, action, data)


async def test_ask_schema_with_a_bare_number_window_is_refused(
    hass: HomeAssistant, loaded: MockConfigEntry
) -> None:
    with pytest.raises(vol.Invalid):
        await act(
            hass,
            "ask",
            {**WINDOWED, "window": {"duration": "00:10:00", "max_events": 3}},
        )


async def test_noul_over_the_token_budget_is_refused_with_what_remains(
    hass: HomeAssistant, fake_client: MagicMock
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, title="tight", data=ENTRY_DATA["typesafe"], options={CONF_TOKEN_BUDGET: 150}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    with pytest.raises(HomeAssistantError) as caught:
        await act(hass, "noul", PLAIN)
    assert caught.value.translation_key == "request_over_budget"
    assert caught.value.translation_placeholders["remaining"] == str(150 - PROBE_TOKENS)
    assert caught.value.translation_placeholders["budget"] == "150"
    assert only_the_probe_was_sent(fake_client)


# --- the server says no ---


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (JevRateLimitError("too many", retry_after=12), "rate_limited"),
        (JevValidationError("instructions too long", status=422), "ask_failed"),
        (JevConnectionError("host unreachable"), "ask_failed"),
    ],
    ids=["rate", "validation", "connection"],
)
async def test_noul_failure_names_the_provider_and_releases_the_reservation(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_client: MagicMock,
    error: Exception,
    key: str,
) -> None:
    fake_client.ask.side_effect = error
    with pytest.raises(HomeAssistantError) as caught:
        await act(hass, "noul", PLAIN)
    assert caught.value.translation_key == key
    assert caught.value.translation_placeholders == {
        "provider": "TypeSafe Jev",
        "reason": str(error),
    }
    assert loaded.runtime_data.ledger.reserved == 0


async def test_box_refused_by_the_server_names_the_entry_model(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_client: MagicMock
) -> None:
    fake_client.ask.side_effect = JevValidationError("Unsupported question type.", status=400)
    with (
        patch(FETCH, AsyncMock(return_value=back_porch())),
        pytest.raises(HomeAssistantError) as caught,
    ):
        await act(hass, "box", {"picture": "camera.back_porch", "instructions": "the cat"})
    assert caught.value.translation_key == "box_refused"
    assert caught.value.translation_placeholders == {
        "model": "jev-latest",
        "reason": "Unsupported question type",
    }


async def test_noul_with_a_refused_key_starts_reauth(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_client: MagicMock
) -> None:
    fake_client.ask.side_effect = JevAuthError("key revoked", status=403)
    with pytest.raises(HomeAssistantError) as caught:
        await act(hass, "noul", PLAIN)
    assert caught.value.translation_key == "key_refused"
    await hass.async_block_till_done()
    sources = [flow["context"]["source"] for flow in hass.config_entries.flow.async_progress()]
    assert sources == [SOURCE_REAUTH]


async def test_score_answered_as_a_choice_is_refused(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_client: MagicMock
) -> None:
    fake_client.ask.return_value = reply(answer=PANTRY)
    with pytest.raises(HomeAssistantError) as caught:
        await act(hass, "score", PLAIN | {"scale": ["Dry", "Damp"]})
    assert caught.value.translation_key == "answer_mismatch"
    assert caught.value.translation_placeholders == {
        "question_type": "score",
        "got": "ChoiceAnswer",
    }


# --- which entry ---


async def test_action_with_nothing_loaded_has_no_entry(
    hass: HomeAssistant, fake_client: MagicMock
) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    with pytest.raises(ServiceValidationError) as caught:
        await act(hass, "noul", PLAIN)
    assert caught.value.translation_key == "no_entry"


async def test_action_with_two_entries_bills_only_the_one_named(
    hass: HomeAssistant, fake_client: MagicMock
) -> None:
    attic = MockConfigEntry(domain=DOMAIN, title="Attic", data=ENTRY_DATA["typesafe"])
    bench = MockConfigEntry(domain=DOMAIN, title="Bench", data=ENTRY_DATA["local_clef"])
    for entry in (bench, attic):
        entry.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError) as caught:
        await act(hass, "noul", PLAIN)
    assert caught.value.translation_key == "entry_ambiguous"
    assert caught.value.translation_placeholders == {"entries": "Attic, Bench"}

    with pytest.raises(ServiceValidationError) as caught:
        await act(hass, "noul", PLAIN | {"config_entry": "01JUNKJUNKJUNK"})
    assert caught.value.translation_key == "no_entry"

    fake_client.ask.return_value = reply(answer=NoulAnswer(noul=0.9))
    await act(hass, "noul", PLAIN | {"config_entry": bench.entry_id})
    assert (bench.runtime_data.ledger.requests, attic.runtime_data.ledger.requests) == (2, 1)


async def test_registering_twice_keeps_the_five_actions(
    hass: HomeAssistant, loaded: MockConfigEntry
) -> None:
    async_register_services(hass)
    actions = hass.services.async_services_for_domain(DOMAIN)
    assert sorted(actions) == ["ask", "box", "choice", "noul", "score"]


# --- templates and who may send what ---

TEMPLATED = {"note": "{{ 17 * 2 }} C", "instructions": "Cool?"}


async def test_template_from_an_automation_or_an_admin_is_rendered(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_client: MagicMock,
    hass_admin_user: MockUser,
) -> None:
    fake_client.ask.return_value = reply(answer=NoulAnswer(noul=0.5))
    await act(hass, "noul", TEMPLATED)
    assert sent(fake_client)[0] == "34 C"
    fake_client.ask.reset_mock()
    await act(hass, "noul", TEMPLATED, hass_admin_user.id)
    assert sent(fake_client)[0] == "34 C"


@pytest.mark.parametrize("who", ["read-only", "vanished"])
async def test_template_from_a_limited_user_goes_as_written(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_client: MagicMock,
    hass_read_only_user: MockUser,
    who: str,
) -> None:
    fake_client.ask.return_value = reply(answer=NoulAnswer(noul=0.5))
    user_id = hass_read_only_user.id if who == "read-only" else "deleted-user-7"
    await act(hass, "noul", TEMPLATED, user_id)
    assert sent(fake_client)[0] == "{{ 17 * 2 }} C"


@pytest.mark.parametrize(
    ("action", "data"),
    [
        ("noul", PLAIN | {"pictures": ["/config/www/porch.jpg"]}),
        ("box", {"picture": "/config/www/porch.jpg", "instructions": "the cat"}),
    ],
    ids=["noul", "box"],
)
async def test_file_path_from_a_user_who_is_no_admin_is_refused(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_client: MagicMock,
    hass_read_only_user: MockUser,
    action: str,
    data: dict[str, Any],
) -> None:
    with pytest.raises(ServiceValidationError) as caught:
        await act(hass, action, data, hass_read_only_user.id)
    assert caught.value.translation_key == "path_needs_admin"
    assert caught.value.translation_placeholders == {"path": "/config/www/porch.jpg"}
    assert only_the_probe_was_sent(fake_client)


async def test_file_path_from_an_admin_still_meets_the_allowlist(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_client: MagicMock,
    hass_admin_user: MockUser,
) -> None:
    hass.config.allowlist_external_dirs = set()
    with pytest.raises(ServiceValidationError) as caught:
        await act(hass, "noul", PLAIN | {"pictures": ["/config/www/porch.jpg"]}, hass_admin_user.id)
    assert caught.value.translation_key == "path_not_allowed"


@pytest.mark.parametrize(
    ("action", "data"),
    [
        ("noul", PLAIN | {"pictures": ["camera.back_porch"]}),
        ("box", {"picture": "camera.back_porch", "instructions": "the cat"}),
    ],
    ids=["noul", "box"],
)
async def test_camera_hidden_from_the_user_is_never_asked_for_a_frame(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_client: MagicMock,
    action: str,
    data: dict[str, Any],
) -> None:
    user = reader_of(hass, DRYER)
    with (
        patch("custom_components.mentat.pictures.camera.async_get_image") as frame,
        pytest.raises(ServiceValidationError) as caught,
    ):
        await act(hass, action, data, user.id)
    assert caught.value.translation_key == "entity_not_permitted"
    assert caught.value.translation_placeholders == {"entity_id": "camera.back_porch"}
    frame.assert_not_called()
    assert only_the_probe_was_sent(fake_client)


async def test_camera_from_a_user_id_that_no_longer_resolves_is_refused(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_client: MagicMock
) -> None:
    with pytest.raises(ServiceValidationError) as caught:
        await act(hass, "noul", PLAIN | {"pictures": ["camera.back_porch"]}, "deleted-user-7")
    assert caught.value.translation_key == "entity_not_permitted"


async def test_source_that_is_no_picture_from_a_limited_user_fails_at_the_fetch(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_client: MagicMock
) -> None:
    user = reader_of(hass)
    with pytest.raises(ServiceValidationError) as caught:
        await act(hass, "noul", PLAIN | {"pictures": ["switch.porch_light"]}, user.id)
    assert caught.value.translation_key == "not_an_image_source"


async def test_target_the_user_may_read_is_sent(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_client: MagicMock
) -> None:
    hass.states.async_set(DRYER, "31")
    user = reader_of(hass, DRYER)
    fake_client.ask.return_value = reply(answer=NoulAnswer(noul=0.5))
    await act(hass, "noul", {"entity_id": DRYER, "instructions": "Cool?"}, user.id)
    assert sent(fake_client)[0]["entities"][0]["entity_id"] == DRYER


async def test_group_target_with_a_hidden_member_is_refused_naming_the_member(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_client: MagicMock
) -> None:
    hass.states.async_set(DRYER, "31")
    hass.states.async_set("cover.garage_door", "open")
    hass.states.async_set("group.garage", "on", {"entity_id": [DRYER, "cover.garage_door"]})
    user = reader_of(hass, DRYER, "group.garage")
    with pytest.raises(ServiceValidationError) as caught:
        await act(hass, "noul", {"entity_id": "group.garage", "instructions": "Cool?"}, user.id)
    assert caught.value.translation_placeholders == {"entity_id": "cover.garage_door"}
    assert only_the_probe_was_sent(fake_client)


async def test_window_on_a_hidden_target_is_refused_before_the_recorder_is_read(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_client: MagicMock,
    mock_recorder: FakeRecorder,
) -> None:
    hass.states.async_set(WASHER, "4.5")
    user = reader_of(hass)
    with pytest.raises(ServiceValidationError) as caught:
        await act(hass, "ask", WINDOWED, user.id)
    assert caught.value.translation_placeholders == {"entity_id": WASHER}
    assert mock_recorder.calls == []
    assert only_the_probe_was_sent(fake_client)


async def test_target_from_an_automation_or_an_admin_is_not_checked(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_client: MagicMock,
    hass_admin_user: MockUser,
) -> None:
    hass.states.async_set(DRYER, "31")
    fake_client.ask.return_value = reply(answer=NoulAnswer(noul=0.5))
    asked = {"entity_id": DRYER, "instructions": "Cool?"}
    await act(hass, "noul", asked)
    await act(hass, "noul", asked, hass_admin_user.id)
    assert fake_client.ask.await_count == 3


async def test_script_runs_as_its_user_and_an_automation_runs_unlimited(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_client: MagicMock
) -> None:
    hass.states.async_set(DRYER, "31")
    fake_client.ask.return_value = reply(answer=NoulAnswer(noul=0.5))
    # Allowed to start both, not allowed to read the dryer.
    user = reader_of(hass, "script.check_dryer", "automation.check_dryer")
    step = {
        "action": "mentat.noul",
        "target": {"entity_id": DRYER},
        "data": {"instructions": "Cool?"},
        "response_variable": "verdict",
    }
    assert await async_setup_component(
        hass, "script", {"script": {"check_dryer": {"sequence": [step]}}}
    )
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "id": "check_dryer",
                "alias": "Check dryer",
                "triggers": [],
                "actions": [step],
            }
        },
    )
    with pytest.raises(ServiceValidationError) as caught:
        await hass.services.async_call(
            "script", "check_dryer", blocking=True, context=Context(user_id=user.id)
        )
    assert caught.value.translation_key == "entity_not_permitted"
    assert only_the_probe_was_sent(fake_client)

    await hass.services.async_call(
        "automation",
        "trigger",
        {"entity_id": "automation.check_dryer"},
        blocking=True,
        context=Context(user_id=user.id),
    )
    await hass.async_block_till_done()
    assert fake_client.ask.await_count == 2


async def test_caller_with_a_template_and_a_target_is_looked_up_once(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_client: MagicMock,
    hass_admin_user: MockUser,
) -> None:
    hass.states.async_set(DRYER, "31")
    fake_client.ask.return_value = reply(answer=NoulAnswer(noul=0.5))
    with patch.object(hass.auth, "async_get_user", wraps=hass.auth.async_get_user) as lookup:
        await act(hass, "noul", TEMPLATED | {"entity_id": DRYER}, hass_admin_user.id)
    lookup.assert_awaited_once_with(hass_admin_user.id)
