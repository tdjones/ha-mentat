"""mentat.box, from a picture source to the four edges, and the Clef dialect saying no.

PIL draws the picture and a patched camera hands it over. The TypeSafe client is
a mock. The Clef client is the real one, which refuses a box before it would
open a connection.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components import camera
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from jevclient import Box, BoxAnswer, Image
from PIL import Image as PILImage
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mentat.client import ClefClient
from custom_components.mentat.const import DOMAIN
from custom_components.mentat.pictures import Pictures
from custom_components.mentat.providers import Dialect

from .conftest import ANSWER_TOKENS, LATENCY_MS, LOCAL_URL, build_response

BIN = BoxAnswer(top=0.35, bottom=0.8, left=0.55, right=0.7, confidence=0.91)


def still(width: int, height: int) -> bytes:
    out = io.BytesIO()
    PILImage.new("RGB", (width, height), (90, 90, 90)).save(out, "JPEG")
    return out.getvalue()


@pytest.fixture
def driveway() -> Any:
    """camera.async_get_image answering with a 2048 x 1536 frame."""
    frame = camera.Image("image/jpeg", still(2048, 1536))
    with patch.object(camera, "async_get_image", AsyncMock(return_value=frame)) as get:
        yield get


async def box(hass: HomeAssistant, data: dict[str, Any]) -> Any:
    return await hass.services.async_call(DOMAIN, "box", data, blocking=True, return_response=True)


async def test_box_on_a_camera_returns_edges_and_the_source_size(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_client: MagicMock,
    driveway: AsyncMock,
) -> None:
    hass.states.async_set("camera.driveway", "idle", {"friendly_name": "Driveway"})
    fake_client.ask.return_value = build_response(answer=BIN)
    reply = await box(
        hass,
        {
            "picture": "camera.driveway",
            "instructions": "the green wheelie bin",
            "brief": "Collection is on Tuesdays.",
        },
    )
    assert reply == {
        "left": 0.55,
        "top": 0.35,
        "right": 0.7,
        "bottom": 0.8,
        "confidence": 0.91,
        "width": 2048,
        "height": 1536,
        "model": "jev-1.14.0",
        "latency_ms": LATENCY_MS,
        "usage": {"input_tokens": ANSWER_TOKENS, "output_tokens": 3},
    }
    state, questions = fake_client.ask.await_args.args
    sent: Pictures = fake_client.ask.await_args.kwargs["pictures"]
    (reference,) = sent.assets
    assert isinstance(sent.assets[reference], Image)
    assert state == {
        "images": [{"name": "Driveway", "entity_id": "camera.driveway", "image": reference}]
    }
    asked = questions["answer"]
    assert isinstance(asked, Box)
    assert asked.instructions == {
        "question": "the green wheelie bin",
        "background": "Collection is on Tuesdays.",
    }
    # Billed at the 1568 x 1176 it was sent at, not the frame's own size.
    assert sent.tokens == 4704
    assert fake_client.ask.await_args.kwargs["model"] is None


async def test_box_on_a_file_returns_that_file_size(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_client: MagicMock,
    tmp_path: Path,
) -> None:
    folder = tmp_path.resolve()
    hass.config.allowlist_external_dirs = {str(folder)}
    path = folder / "drive.jpg"
    path.write_bytes(still(720, 540))
    fake_client.ask.return_value = build_response(answer=BIN)
    reply = await box(hass, {"picture": str(path), "instructions": "the bin"})
    assert (reply["width"], reply["height"]) == (720, 540)
    assert fake_client.ask.await_args.args[0]["images"][0]["path"] == str(path)


async def test_box_on_a_switch_is_refused_unsent(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_client: MagicMock
) -> None:
    with pytest.raises(HomeAssistantError) as caught:
        await box(hass, {"picture": "switch.driveway_light", "instructions": "the bin"})
    assert caught.value.translation_key == "not_an_image_source"
    # The setup probe is the only request.
    assert fake_client.ask.await_count == 1


@pytest.mark.parametrize("provider_key", ["local_clef"], indirect=True)
async def test_box_on_a_clef_entry_is_refused_before_posting(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_client: MagicMock,
    driveway: AsyncMock,
) -> None:
    session = MagicMock()
    real = ClefClient("", session=session, url=LOCAL_URL, model="clef-flash")
    fake_client.dialect = Dialect.CLEF
    fake_client.ask.side_effect = real.ask
    with pytest.raises(HomeAssistantError) as caught:
        await box(hass, {"picture": "camera.driveway", "instructions": "the bin"})
    assert caught.value.translation_key == "box_refused"
    assert caught.value.translation_placeholders["model"] == "clef-flash"
    assert "no box question" in caught.value.translation_placeholders["reason"]
    session.post.assert_not_called()
    assert loaded.runtime_data.ledger.reserved == 0
