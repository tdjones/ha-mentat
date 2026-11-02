"""The AI Task entity: a script's structured task asked in one request and answered by field."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components import ai_task, camera, conversation
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from jevclient import (
    BoxAnswer,
    ChoiceAnswer,
    JevAuthError,
    JevConnectionError,
    NoulAnswer,
    ScoreAnswer,
)
from PIL import Image as PILImage
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mentat.ai_task import MentatAITaskEntity, _certainty_of
from custom_components.mentat.const import CONF_MAX_IMAGES, CONF_TOKEN_BUDGET, DOMAIN
from custom_components.mentat.pictures import Pictures
from custom_components.mentat.providers import Dialect

from .conftest import ANSWER_TOKENS, ENTRY_DATA, LATENCY_MS, PROBE_TOKENS, build_response

KERB = "The kerb outside, Tuesday at seven."
BINS_OUT = {
    "bins_out": {
        "description": "Are the bins out at the kerb?",
        "selector": {"boolean": {}},
        "required": True,
    }
}
SKY = {
    "sky": {
        "description": "What is the sky doing?",
        "selector": {"select": {"options": ["clear", "cloud", "rain"]}},
    }
}
FULLNESS = {
    "fullness": {
        "description": "How full is the green bin, 0 to 100?",
        "selector": {"number": {"min": 0, "max": 100, "step": 20}},
    }
}
DRIVEWAY = {
    "media_content_id": "media-source://camera/camera.driveway",
    "media_content_type": "image/jpeg",
}


def jpeg(width: int, height: int) -> bytes:
    out = io.BytesIO()
    PILImage.new("RGB", (width, height), (30, 110, 60)).save(out, "JPEG")
    return out.getvalue()


def task_entity_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    found = er.async_get(hass).async_get_entity_id("ai_task", DOMAIN, f"{entry.entry_id}:ai_task")
    assert found is not None
    return found


async def run_task(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    structure: dict[str, Any] | None,
    instructions: str = KERB,
    **more: Any,
) -> Any:
    data: dict[str, Any] = {
        "entity_id": task_entity_id(hass, entry),
        "task_name": "bin night",
        "instructions": instructions,
        **more,
    }
    if structure is not None:
        data["structure"] = structure
    return await hass.services.async_call(
        "ai_task", "generate_data", data, blocking=True, return_response=True
    )


async def _entry_with(hass: HomeAssistant, **options: Any) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title="typesafe", data=dict(ENTRY_DATA["typesafe"]), options=options
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture
def frame() -> Any:
    """camera.async_get_image answering with a 2048 x 1536 frame."""
    still = camera.Image("image/jpeg", jpeg(2048, 1536))
    with patch.object(camera, "async_get_image", AsyncMock(return_value=still)) as get:
        yield get


def _bare_task(*attachments: conversation.Attachment) -> ai_task.GenDataTask:
    return ai_task.GenDataTask(name="bin night", instructions=KERB, attachments=list(attachments))


def _bare_entity(hass: HomeAssistant, entry: MockConfigEntry) -> MentatAITaskEntity:
    """The entity outside the platform, to call _async_attachments directly."""
    entity = MentatAITaskEntity(entry)  # type: ignore[arg-type]
    entity.hass = hass
    return entity


def _attachment(
    path: Path, media_id: str, mime_type: str = "image/jpeg"
) -> conversation.Attachment:
    return conversation.Attachment(media_content_id=media_id, mime_type=mime_type, path=path)


# The entity


async def test_entity_joins_the_service_device_with_both_features(
    hass: HomeAssistant, loaded: MockConfigEntry
) -> None:
    entity_id = task_entity_id(hass, loaded)
    state = hass.states.get(entity_id)
    assert state is not None
    # GENERATE_DATA (1) and SUPPORT_ATTACHMENTS (2).
    assert state.attributes["supported_features"] == 3
    registered = er.async_get(hass).async_get(entity_id)
    assert registered is not None
    assert registered.translation_key == "decision"
    device = dr.async_get(hass).async_get(registered.device_id or "")
    assert device is not None
    assert device.identifiers == {(DOMAIN, loaded.entry_id)}


# Answers


async def test_task_boolean_field_returns_a_bool(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_client: MagicMock
) -> None:
    fake_client.ask.return_value = build_response(bins_out=NoulAnswer(noul=0.66))
    result = await run_task(hass, loaded, BINS_OUT)
    assert result["data"]["bins_out"] is True
    assert result["conversation_id"]
    state, questions = fake_client.ask.await_args.args
    assert state == KERB
    assert questions["bins_out"].instructions == "Are the bins out at the kerb?"
    assert fake_client.ask.await_args.kwargs == {"model": None, "pictures": None}


async def test_task_with_every_field_type_returns_values_and_a_receipt(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_client: MagicMock
) -> None:
    fake_client.ask.return_value = build_response(
        bins_out=NoulAnswer(noul=0.31819),
        sky=ChoiceAnswer(
            choice="rain",
            probabilities={"clear": 0.05, "cloud": 0.15, "rain": 0.8},
            confidence=0.72,
        ),
        fullness=ScoreAnswer(
            score=4.0,
            legend={str(rung): str(rung * 20) for rung in range(6)},
            probabilities={"4": 0.75, "3": 0.25},
            confidence=0.55,
        ),
    )
    result = await run_task(hass, loaded, BINS_OUT | SKY | FULLNESS)
    data = result["data"]
    assert (data["bins_out"], data["sky"], data["fullness"]) == (False, "rain", 80)
    assert data["mentat"] == {
        "model": "jev-1.14.0",
        "input_tokens": ANSWER_TOKENS,
        "latency_ms": round(LATENCY_MS),
        "answers": {
            "bins_out": {"probability": 0.3182},
            "sky": {
                "confidence": 0.72,
                "probabilities": {"clear": 0.05, "cloud": 0.15, "rain": 0.8},
            },
            "fullness": {
                "confidence": 0.55,
                "probabilities": {"4": 0.75, "3": 0.25},
                "nearest_level": "80",
            },
        },
    }
    assert loaded.runtime_data.ledger.tokens == PROBE_TOKENS + ANSWER_TOKENS


def test_certainty_of_a_box_is_empty() -> None:
    assert _certainty_of(BoxAnswer(top=0.2, bottom=0.6, left=0.1, right=0.5, confidence=0.9)) == {}


# Refusals


async def test_task_without_a_structure_is_refused_unsent(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_client: MagicMock
) -> None:
    with pytest.raises(ServiceValidationError) as caught:
        await run_task(hass, loaded, None)
    assert caught.value.translation_key == "task_needs_structure"
    assert caught.value.translation_placeholders == {"task": "bin night"}
    assert fake_client.ask.await_count == 1


async def test_task_with_a_text_field_is_refused_unsent(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_client: MagicMock
) -> None:
    notes = {"notes": {"description": "Anything else?", "selector": {"text": {}}}}
    with pytest.raises(ServiceValidationError) as caught:
        await run_task(hass, loaded, BINS_OUT | notes)
    assert caught.value.translation_key == "unsupported_field"
    assert caught.value.translation_placeholders == {"field": "notes", "selector": "TextSelector"}
    assert fake_client.ask.await_count == 1


async def test_task_over_budget_is_refused_by_name(
    hass: HomeAssistant, fake_client: MagicMock
) -> None:
    entry = await _entry_with(hass, **{CONF_TOKEN_BUDGET: 150})
    with pytest.raises(HomeAssistantError) as caught:
        await run_task(hass, entry, BINS_OUT)
    assert caught.value.translation_key == "task_over_budget"
    assert caught.value.translation_placeholders["task"] == "bin night"
    assert caught.value.translation_placeholders["remaining"] == str(150 - PROBE_TOKENS)
    assert fake_client.ask.await_count == 1


async def test_task_server_failure_carries_the_reason(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_client: MagicMock
) -> None:
    fake_client.ask.side_effect = JevConnectionError("timed out after 30 s")
    with pytest.raises(HomeAssistantError) as caught:
        await run_task(hass, loaded, BINS_OUT)
    assert caught.value.translation_key == "ask_failed"
    assert caught.value.translation_placeholders["reason"] == "timed out after 30 s"


async def test_task_with_a_refused_key_starts_reauth(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_client: MagicMock
) -> None:
    fake_client.ask.side_effect = JevAuthError("key revoked", status=401)
    with pytest.raises(HomeAssistantError) as caught:
        await run_task(hass, loaded, BINS_OUT)
    assert caught.value.translation_key == "key_refused"
    await hass.async_block_till_done()
    sources = [flow["context"]["source"] for flow in hass.config_entries.flow.async_progress()]
    assert sources == [SOURCE_REAUTH]


async def test_task_answer_missing_a_field_is_refused(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_client: MagicMock
) -> None:
    fake_client.ask.return_value = build_response(something_else=NoulAnswer(noul=0.4))
    with pytest.raises(ServiceValidationError) as caught:
        await run_task(hass, loaded, BINS_OUT)
    assert caught.value.translation_key == "answer_missing"


# Attachments


async def test_task_camera_attachment_goes_beside_the_instructions(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_client: MagicMock, frame: AsyncMock
) -> None:
    fake_client.ask.return_value = build_response(bins_out=NoulAnswer(noul=0.92))
    result = await run_task(hass, loaded, BINS_OUT, attachments=[DRIVEWAY])
    assert result["data"]["bins_out"] is True
    state = fake_client.ask.await_args.args[0]
    sent: Pictures = fake_client.ask.await_args.kwargs["pictures"]
    (reference,) = sent.assets
    assert state == {"images": [{"name": "camera.driveway", "image": reference}], "note": KERB}
    assert sent.ordered[0].sent == (1568, 1176)
    assert sent.tokens == 4704


async def test_task_on_a_spent_budget_reads_no_attachment(
    hass: HomeAssistant, fake_client: MagicMock, frame: AsyncMock
) -> None:
    entry = await _entry_with(hass, **{CONF_TOKEN_BUDGET: 150})
    with (
        patch("custom_components.mentat.ai_task.async_read_file", AsyncMock()) as read,
        pytest.raises(HomeAssistantError) as caught,
    ):
        await run_task(hass, entry, BINS_OUT, attachments=[DRIVEWAY])
    assert caught.value.translation_key == "task_over_budget"
    assert caught.value.translation_placeholders["task"] == "bin night"
    read.assert_not_awaited()
    assert fake_client.ask.await_count == 1


@pytest.mark.parametrize("provider_key", ["local_clef"], indirect=True)
async def test_task_on_a_clef_entry_numbers_the_attachments(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_client: MagicMock, frame: AsyncMock
) -> None:
    fake_client.dialect = Dialect.CLEF
    fake_client.ask.return_value = build_response(bins_out=NoulAnswer(noul=0.2))
    await run_task(hass, loaded, BINS_OUT, instructions="", attachments=[DRIVEWAY, DRIVEWAY])
    assert fake_client.ask.await_args.args[0] == {
        "images": [
            {"index": 1, "name": "camera.driveway"},
            {"index": 2, "name": "camera.driveway"},
        ]
    }
    sent: Pictures = fake_client.ask.await_args.kwargs["pictures"]
    assert sent.assets == {}
    assert [picture.sent for picture in sent.ordered] == [(1024, 768), (1024, 768)]


async def test_attachments_read_a_local_file_by_its_name(
    hass: HomeAssistant, loaded: MockConfigEntry, tmp_path: Path
) -> None:
    photo = tmp_path / "kerb_morning.jpg"
    photo.write_bytes(jpeg(600, 400))
    media_id = "media-source://media_source/local/bins/kerb_morning.jpg"
    pictures = await _bare_entity(hass, loaded)._async_attachments(
        _bare_task(_attachment(photo, media_id))
    )
    assert pictures.entries[0]["name"] == "kerb_morning.jpg"
    assert pictures.ordered[0].sent == (600, 400)


async def test_attachments_are_read_together_and_kept_in_order(
    hass: HomeAssistant, loaded: MockConfigEntry, tmp_path: Path
) -> None:
    """Each read waits for the other, so reading them in turn would never finish."""
    started: list[str] = []
    both = asyncio.Event()

    async def read(_hass: HomeAssistant, path: Path, source: str) -> bytes:
        started.append(source)
        if len(started) == 2:
            both.set()
        await asyncio.wait_for(both.wait(), timeout=1)
        return jpeg(300, 300) if path.name == "front.jpg" else jpeg(600, 400)

    files = [
        _attachment(tmp_path / name, f"media-source://local/{name}")
        for name in ("front.jpg", "back.jpg")
    ]
    with patch("custom_components.mentat.ai_task.async_read_file", read):
        pictures = await _bare_entity(hass, loaded)._async_attachments(_bare_task(*files))
    assert [entry["name"] for entry in pictures.entries] == ["front.jpg", "back.jpg"]
    assert [picture.source for picture in pictures.ordered] == [(300, 300), (600, 400)]


async def test_attachments_refuse_a_video(
    hass: HomeAssistant, loaded: MockConfigEntry, tmp_path: Path
) -> None:
    clip = tmp_path / "doorbell.mp4"
    clip.write_bytes(b"\x00\x00\x00\x20ftypisom")
    media_id = "media-source://media_source/local/doorbell.mp4"
    with pytest.raises(ServiceValidationError) as caught:
        await _bare_entity(hass, loaded)._async_attachments(
            _bare_task(_attachment(clip, media_id, "video/mp4"))
        )
    assert caught.value.translation_key == "attachment_not_image"
    assert caught.value.translation_placeholders == {
        "attachment": media_id,
        "mime_type": "video/mp4",
    }


async def test_attachments_read_a_vanished_file_as_unreadable(
    hass: HomeAssistant, loaded: MockConfigEntry, tmp_path: Path
) -> None:
    missing = _attachment(tmp_path / "deleted.jpg", "media-source://media_source/local/deleted.jpg")
    with pytest.raises(HomeAssistantError) as caught:
        await _bare_entity(hass, loaded)._async_attachments(_bare_task(missing))
    assert caught.value.translation_key == "image_unreadable"


async def test_attachments_stop_reading_at_the_file_cap(
    hass: HomeAssistant, loaded: MockConfigEntry, tmp_path: Path
) -> None:
    """A media source can resolve to a video, so only the cap's worth is read."""
    big = tmp_path / "long.jpg"
    big.write_bytes(jpeg(600, 400))
    media_id = "media-source://media_source/local/long.jpg"
    with (
        patch("custom_components.mentat.pictures.MAX_SOURCE_FILE_BYTES", 80),
        pytest.raises(HomeAssistantError) as caught,
    ):
        await _bare_entity(hass, loaded)._async_attachments(_bare_task(_attachment(big, media_id)))
    assert caught.value.translation_key == "image_too_large"
    assert caught.value.translation_placeholders == {
        "source": media_id,
        "size": f"{big.stat().st_size:,} bytes",
        "limit": "80 bytes",
    }


async def test_attachments_over_the_entry_limit_are_refused(
    hass: HomeAssistant, fake_client: MagicMock, tmp_path: Path
) -> None:
    entry = await _entry_with(hass, **{CONF_MAX_IMAGES: 2})
    one = _attachment(tmp_path / "x.jpg", "media-source://camera/camera.driveway")
    with pytest.raises(ServiceValidationError) as caught:
        await _bare_entity(hass, entry)._async_attachments(_bare_task(one, one, one))
    assert caught.value.translation_key == "too_many_images"
    assert caught.value.translation_placeholders == {"count": "3", "limit": "2"}
