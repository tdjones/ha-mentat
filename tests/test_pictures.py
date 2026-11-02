"""Pictures: read from a camera, an image entity or a file, then sized and billed per dialect.

PIL draws every picture inside the test, and the client is a mock, so these
tests look at what would be sent and what the budget was charged for it.
"""

from __future__ import annotations

import asyncio
import io
import struct
import warnings
import zlib
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components import camera, image
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from jevclient import Image, NoulAnswer
from PIL import Image as PILImage
from PIL.JpegImagePlugin import JpegImageFile
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mentat import pictures as pictures_module
from custom_components.mentat.const import CONF_MAX_IMAGES, CONF_TOKEN_BUDGET, DOMAIN
from custom_components.mentat.pictures import (
    Picture,
    Pictures,
    async_request_images,
    attach,
    label_for_entity,
    label_for_path,
    prepare_image,
)
from custom_components.mentat.providers import Dialect, ImageLimits, image_limits

from .conftest import ENTRY_DATA, PROBE_TOKENS, build_response

TYPESAFE = image_limits(Dialect.TYPESAFE)
CLEF = image_limits(Dialect.CLEF)
FRAME = (2048, 1536)


def drawn(size: tuple[int, int], fmt: str = "JPEG", **save: Any) -> bytes:
    """A flat blue picture of this size in this format."""
    out = io.BytesIO()
    PILImage.new("RGB", size, (20, 40, 200)).save(out, fmt, **save)
    return out.getvalue()


def static(size: tuple[int, int]) -> bytes:
    """Random pixels as PNG, which JPEG cannot squeeze much."""
    out = io.BytesIO()
    PILImage.effect_noise(size, 96).convert("RGB").save(out, "PNG")
    return out.getvalue()


def lying_png(width: int, height: int) -> bytes:
    """One pixel of PNG whose IHDR claims another size, the way a bomb's does."""
    data = bytearray(drawn((1, 1), "PNG"))
    # 8 bytes of signature, then the IHDR length and type, then its width and height.
    data[16:24] = struct.pack(">II", width, height)
    data[29:33] = struct.pack(">I", zlib.crc32(bytes(data[12:29])))
    return bytes(data)


def decoded(picture: Picture) -> PILImage.Image:
    return PILImage.open(io.BytesIO(picture.payload.data))


def turned(orientation: int) -> PILImage.Exif:
    exif = PILImage.Exif()
    exif[0x0112] = orientation
    return exif


def placeholder(sent: tuple[int, int]) -> Picture:
    """A Picture that never went through PIL, for the bookkeeping tests."""
    return Picture(payload=Image(b"\xff\xd8 stand-in", "jpg"), sent=sent, source=sent)


# Preparing one picture


@pytest.mark.parametrize(
    ("limits", "source", "sent"),
    [
        (TYPESAFE, (2048, 1536), (1568, 1176)),
        (TYPESAFE, (150, 100), (420, 280)),
        (TYPESAFE, (800, 600), (800, 600)),
        (CLEF, (1600, 1200), (1024, 768)),
        (CLEF, (900, 1600), (576, 1024)),
        (CLEF, (500, 400), (500, 400)),
        (CLEF, (20, 16), (40, 32)),
    ],
)
def test_prepare_image_sizes_for_the_dialect(
    limits: ImageLimits, source: tuple[int, int], sent: tuple[int, int]
) -> None:
    picture = prepare_image(drawn(source), "camera.driveway", limits)
    assert picture.sent == sent
    assert picture.source == source
    assert picture.payload.format == "jpg"
    out = decoded(picture)
    assert (out.format, out.size) == ("JPEG", sent)
    assert "progressive" not in out.info


def test_prepare_image_stretches_the_short_side_of_a_panorama() -> None:
    """A box is a fraction of what was sent, so the rows must keep their fraction."""
    strip = PILImage.new("RGB", (6000, 300), "white")
    strip.paste((200, 0, 0), (0, 0, 6000, 100))
    out = io.BytesIO()
    strip.save(out, "PNG")
    picture = prepare_image(out.getvalue(), "camera.yard", TYPESAFE)
    assert picture.sent == (1568, 280)
    pixels = decoded(picture).convert("RGB")
    # The red third is the top 93 rows of 280.
    assert pixels.getpixel((700, 5))[2] < 60
    assert pixels.getpixel((700, 85))[2] < 60
    assert pixels.getpixel((700, 105))[2] > 200


@pytest.mark.parametrize("limits", [TYPESAFE, CLEF], ids=["typesafe", "clef"])
def test_prepare_image_turns_by_exif_orientation(limits: ImageLimits) -> None:
    picture = prepare_image(drawn((1200, 600), exif=turned(6)), "phone", limits)
    assert picture.source == (600, 1200)
    assert picture.sent[1] > picture.sent[0]


@pytest.mark.parametrize("limits", [TYPESAFE, CLEF], ids=["typesafe", "clef"])
def test_prepare_image_puts_transparency_on_white(limits: ImageLimits) -> None:
    out = io.BytesIO()
    PILImage.new("RGBA", (360, 360), (0, 120, 0, 0)).save(out, "PNG")
    pixel = decoded(prepare_image(out.getvalue(), "image.badge", limits)).getpixel((180, 180))
    assert pixel == pytest.approx((255, 255, 255), abs=8)


def test_prepare_image_puts_a_transparent_palette_on_white() -> None:
    out = io.BytesIO()
    PILImage.new("P", (320, 320), 3).save(out, "GIF", transparency=3)
    pixel = decoded(prepare_image(out.getvalue(), "image.sprite", TYPESAFE)).getpixel((40, 40))
    assert pixel == pytest.approx((255, 255, 255), abs=8)


def test_prepare_image_names_the_source_of_bytes_that_are_no_picture() -> None:
    with pytest.raises(HomeAssistantError) as caught:
        prepare_image(b"502 Bad Gateway", "camera.shed", TYPESAFE)
    assert caught.value.translation_domain == DOMAIN
    assert caught.value.translation_key == "image_unreadable"
    assert caught.value.translation_placeholders["source"] == "camera.shed"


def test_prepare_image_refuses_a_source_over_the_pixel_limit() -> None:
    limits = ImageLimits(32, 1024, None, 4 * 1024 * 1024, max_source_pixels=20_000)
    with pytest.raises(HomeAssistantError) as caught:
        prepare_image(drawn((300, 100)), "/media/wide.jpg", limits)
    assert caught.value.translation_key == "image_too_large"
    assert caught.value.translation_placeholders == {
        "source": "/media/wide.jpg",
        "size": "300 x 100 pixels",
        "limit": "20,000 pixels",
    }


@pytest.mark.parametrize("limits", [TYPESAFE, CLEF], ids=["typesafe", "clef"])
def test_dialect_limits_refuse_a_forged_header(limits: ImageLimits) -> None:
    """A header claiming 9000 x 9000 costs a few dozen bytes to write."""
    assert limits.max_source_pixels <= 50_000_000
    with pytest.raises(HomeAssistantError) as caught:
        prepare_image(lying_png(9000, 9000), "/media/claim.png", limits)
    assert caught.value.translation_key == "image_too_large"
    assert caught.value.translation_placeholders["size"] == "9000 x 9000 pixels"


def test_prepare_image_refuses_what_pillow_only_warns_about() -> None:
    before = list(warnings.filters)
    with pytest.raises(HomeAssistantError) as caught:
        prepare_image(lying_png(11_000, 11_000), "/media/claim.png", TYPESAFE)
    assert caught.value.translation_placeholders["size"] == "11000 x 11000 pixels"
    assert warnings.filters == before


def test_prepare_image_reports_a_decompression_bomb_as_too_large() -> None:
    with pytest.raises(HomeAssistantError) as caught:
        prepare_image(lying_png(15_000, 15_000), "/media/bomb.png", TYPESAFE)
    assert caught.value.translation_key == "image_too_large"
    assert caught.value.translation_placeholders == {
        "source": "/media/bomb.png",
        "size": f"more than {2 * PILImage.MAX_IMAGE_PIXELS:,} pixels",
        "limit": "50,000,000 pixels",
    }


# Two EXIF blocks a fuzzer found in a turned picture's IFD. Pillow raises
# struct.error on the first and TypeError on the second.
_STRUCT_ERROR_EXIF = bytes.fromhex(
    "45786966000049492a000800000004001201030001000000060000001a010400020000001a"
    "00000025880900010000000000000005a00100030000001a00000000000000126b78d5b604"
    "489493213626299bc560b810a29672ea47a5157e7acd"
)
_TYPE_ERROR_EXIF = bytes.fromhex(
    "45786966000049492a000800000003001201030001000000060000006987020002000000"
    "000000001a010100030000003c000000000000004878a3e6265c5033ed7f18f824c83454"
    "40246b5c401657303fb53dba9c0b56f05fa5"
)


@pytest.mark.parametrize("exif", [_STRUCT_ERROR_EXIF, _TYPE_ERROR_EXIF], ids=["struct", "type"])
def test_prepare_image_reads_a_corrupt_exif_block_as_unreadable(exif: bytes) -> None:
    jpeg = drawn((80, 60))
    app1 = b"\xff\xe1" + (len(exif) + 2).to_bytes(2, "big") + exif
    with pytest.raises(HomeAssistantError) as caught:
        prepare_image(jpeg[:2] + app1 + jpeg[2:], "/media/fuzzed.jpg", CLEF)
    assert caught.value.translation_key == "image_unreadable"
    assert caught.value.translation_placeholders["source"] == "/media/fuzzed.jpg"


def test_prepare_image_decodes_a_large_jpeg_at_its_sent_size() -> None:
    with patch.object(
        JpegImageFile, "draft", autospec=True, side_effect=JpegImageFile.draft
    ) as draft:
        picture = prepare_image(drawn((3264, 2448)), "/media/phone.jpg", CLEF)
    assert draft.call_args.args[1:] == (None, (1024, 768))
    assert picture.sent == (1024, 768)
    assert picture.source == (3264, 2448)


def test_prepare_image_reports_a_turned_jpeg_upright() -> None:
    picture = prepare_image(drawn((3000, 1500), exif=turned(6)), "phone.jpg", CLEF)
    assert picture.source == (1500, 3000)
    assert picture.sent == (512, 1024)


def test_prepare_image_shrinks_until_under_the_byte_cap() -> None:
    data = static((700, 500))
    roomy = prepare_image(data, "x", ImageLimits(32, 1024, None, 10**8, 10**8))
    limits = ImageLimits(32, 1024, None, len(roomy.payload.data) // 3, 10**8)
    picture = prepare_image(data, "camera.driveway", limits)
    assert len(picture.payload.data) <= limits.max_bytes
    width, height = picture.sent
    assert width < 700
    assert width / height == pytest.approx(1.4, rel=0.02)


def test_prepare_image_refuses_what_never_fits_the_byte_cap() -> None:
    with pytest.raises(HomeAssistantError) as caught:
        prepare_image(
            static((240, 240)), "camera.driveway", ImageLimits(32, 1024, None, 200, 10**8)
        )
    assert caught.value.translation_key == "image_too_large"
    assert caught.value.translation_placeholders["limit"] == "200 bytes"


def test_prepare_image_honours_a_pixel_cap_tighter_than_the_sides() -> None:
    limits = ImageLimits(32, 4096, 120 * 120, 10**8, 10**8)
    assert prepare_image(drawn((480, 480)), "x", limits).sent == (120, 120)


# Pictures and the state


def test_pictures_for_typesafe_refer_to_each_asset_by_uuid() -> None:
    pictures = Pictures.empty(Dialect.TYPESAFE)
    pictures.add({"name": "Driveway", "entity_id": "camera.driveway"}, placeholder((1568, 1176)))
    pictures.add({"name": "before.jpg", "path": "/media/before.jpg"}, placeholder((280, 280)))
    driveway, before = pictures.entries
    assert driveway["entity_id"] == "camera.driveway"
    assert before["path"] == "/media/before.jpg"
    assert len(driveway["image"]) == 36
    assert driveway["image"] != before["image"]
    assert list(pictures.assets) == [driveway["image"], before["image"]]
    assert [p.sent for p in pictures.ordered] == [(1568, 1176), (280, 280)]
    # Two tokens per 28 px patch: 56 x 42, then 10 x 10.
    assert pictures.tokens == 4704 + 200
    assert pictures


def test_pictures_for_clef_count_from_one() -> None:
    pictures = Pictures.empty(Dialect.CLEF)
    pictures.add({"name": "before.jpg", "path": "/media/before.jpg"}, placeholder((1024, 768)))
    pictures.add({"name": "Driveway", "entity_id": "camera.driveway"}, placeholder((640, 480)))
    assert pictures.entries == [
        {"index": 1, "name": "before.jpg", "path": "/media/before.jpg"},
        {"index": 2, "name": "Driveway", "entity_id": "camera.driveway"},
    ]
    assert pictures.assets == {}
    # One token per 32 px patch and three more: 32 x 24 + 3, then 20 x 15 + 3.
    assert pictures.tokens == 771 + 303


@pytest.mark.parametrize("dialect", list(Dialect))
def test_pictures_with_nothing_added_are_false(dialect: Dialect) -> None:
    assert not Pictures.empty(dialect)


@pytest.mark.parametrize(
    ("state", "shaped"),
    [
        ({"now": "06:10", "entities": [1]}, {"now": "06:10", "entities": [1], "images": None}),
        ("The car left at eight.", {"images": None, "note": "The car left at eight."}),
        (["gate", "drive"], {"images": None, "note": ["gate", "drive"]}),
        ("", {"images": None}),
        (None, {"images": None}),
    ],
)
def test_attach_shapes_the_state(state: Any, shaped: dict[str, Any]) -> None:
    pictures = Pictures.empty(Dialect.TYPESAFE)
    pictures.add({"name": "Driveway"}, placeholder((800, 600)))
    shaped["images"] = pictures.entries
    assert attach(state, pictures) == shaped
    assert list(attach(state, pictures)) == list(shaped)


@pytest.mark.parametrize("pictures", [None, Pictures.empty(Dialect.CLEF)])
def test_attach_without_pictures_returns_the_state(pictures: Pictures | None) -> None:
    assert attach("The car left at eight.", pictures) == "The car left at eight."


def test_label_for_entity_falls_back_to_the_id(hass: HomeAssistant) -> None:
    hass.states.async_set("camera.driveway", "idle", {"friendly_name": "Driveway"})
    assert label_for_entity(hass, "camera.driveway") == {
        "name": "Driveway",
        "entity_id": "camera.driveway",
    }
    assert label_for_entity(hass, "camera.removed") == {
        "name": "camera.removed",
        "entity_id": "camera.removed",
    }


def test_label_for_path_is_the_file_name() -> None:
    assert label_for_path("/media/garden/before.jpg") == {
        "name": "before.jpg",
        "path": "/media/garden/before.jpg",
    }


# Fetching


@pytest.fixture
def frame() -> Any:
    """camera.async_get_image answering with a 2048 x 1536 JPEG."""
    still = camera.Image("image/jpeg", drawn(FRAME))
    with patch.object(camera, "async_get_image", AsyncMock(return_value=still)) as get:
        yield get


@pytest.fixture
def media(hass: HomeAssistant, tmp_path: Path) -> Path:
    """An allowed folder holding one 500 x 400 PNG named before.png."""
    folder = tmp_path.resolve()
    hass.config.allowlist_external_dirs = {str(folder)}
    (folder / "before.png").write_bytes(drawn((500, 400), "PNG"))
    return folder


async def test_request_images_in_the_typesafe_shape(
    hass: HomeAssistant, frame: AsyncMock, media: Path
) -> None:
    hass.states.async_set("camera.driveway", "idle", {"friendly_name": "Driveway"})
    path = str(media / "before.png")
    pictures = await async_request_images(hass, [path, "camera.driveway"], Dialect.TYPESAFE)
    frame.assert_awaited_once_with(hass, "camera.driveway", timeout=10)
    first, second = pictures.entries
    assert first == {"name": "before.png", "path": path, "image": first["image"]}
    assert second == {"name": "Driveway", "entity_id": "camera.driveway", "image": second["image"]}
    assert list(pictures.assets) == [first["image"], second["image"]]
    assert [p.sent for p in pictures.ordered] == [(500, 400), (1568, 1176)]
    assert [p.source for p in pictures.ordered] == [(500, 400), FRAME]
    assert pictures.tokens == 540 + 4704


async def test_request_images_in_the_clef_shape(
    hass: HomeAssistant, frame: AsyncMock, media: Path
) -> None:
    path = str(media / "before.png")
    pictures = await async_request_images(hass, [path, "camera.driveway"], Dialect.CLEF)
    assert pictures.entries == [
        {"index": 1, "name": "before.png", "path": path},
        {"index": 2, "name": "camera.driveway", "entity_id": "camera.driveway"},
    ]
    assert pictures.assets == {}
    assert [p.sent for p in pictures.ordered] == [(500, 400), (1024, 768)]
    assert pictures.tokens == 211 + 771


async def test_request_images_prepares_together_and_keeps_the_order(
    hass: HomeAssistant, frame: AsyncMock, media: Path
) -> None:
    path = str(media / "before.png")
    begun: list[str] = []
    both = asyncio.Event()
    original = pictures_module.async_prepare

    async def gated(hass: HomeAssistant, data: bytes, source: str, dialect: Dialect) -> Picture:
        begun.append(source)
        if len(begun) == 2:
            both.set()
        # Neither may finish until both have started, which run one at a time
        # would never reach.
        await asyncio.wait_for(both.wait(), 1)
        return await original(hass, data, source, dialect)

    with patch.object(pictures_module, "async_prepare", gated):
        pictures = await async_request_images(hass, [path, "camera.driveway"], Dialect.CLEF)
    assert set(begun) == {path, "camera.driveway"}
    assert [entry["index"] for entry in pictures.entries] == [1, 2]
    assert pictures.entries[0]["path"] == path
    assert [p.sent for p in pictures.ordered] == [(500, 400), (1024, 768)]


async def test_request_images_reads_an_image_entity(hass: HomeAssistant) -> None:
    still = image.Image("image/png", drawn((600, 450), "PNG"))
    with patch.object(image, "async_get_image", AsyncMock(return_value=still)) as get:
        pictures = await async_request_images(hass, ["image.gate_snapshot"], Dialect.TYPESAFE)
    get.assert_awaited_once_with(hass, "image.gate_snapshot", timeout=10)
    assert pictures.ordered[0].sent == (600, 450)


async def test_request_images_names_a_camera_that_fails(hass: HomeAssistant) -> None:
    broken = AsyncMock(side_effect=HomeAssistantError("Stream timed out"))
    with (
        patch.object(camera, "async_get_image", broken),
        pytest.raises(HomeAssistantError) as caught,
    ):
        await async_request_images(hass, ["camera.shed"], Dialect.TYPESAFE)
    assert caught.value.translation_key == "image_unavailable"
    assert caught.value.translation_placeholders == {
        "entity_id": "camera.shed",
        "reason": "Stream timed out",
    }


@pytest.mark.parametrize("source", ["switch.porch", "media/porch.png", "porch.png", "image", ""])
async def test_request_images_refuses_what_is_no_source(hass: HomeAssistant, source: str) -> None:
    with pytest.raises(ServiceValidationError) as caught:
        await async_request_images(hass, [source], Dialect.TYPESAFE)
    assert caught.value.translation_key == "not_an_image_source"
    assert caught.value.translation_placeholders == {"source": source}


async def test_request_images_refuses_a_path_outside_the_allowlist(
    hass: HomeAssistant, media: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    hidden = tmp_path_factory.mktemp("elsewhere").resolve() / "private.png"
    hidden.write_bytes(drawn((300, 300), "PNG"))
    with pytest.raises(ServiceValidationError) as caught:
        await async_request_images(hass, [str(hidden)], Dialect.TYPESAFE)
    assert caught.value.translation_key == "path_not_allowed"
    assert caught.value.translation_placeholders == {"path": str(hidden)}


@pytest.mark.parametrize("name", ["absent.png", "readme.txt", "."])
async def test_request_images_reads_an_allowed_non_picture_as_unreadable(
    hass: HomeAssistant, media: Path, name: str
) -> None:
    (media / "readme.txt").write_text("plain words")
    path = str(media / name)
    with pytest.raises(HomeAssistantError) as caught:
        await async_request_images(hass, [path], Dialect.TYPESAFE)
    assert caught.value.translation_key == "image_unreadable"
    assert caught.value.translation_placeholders["source"] == path


async def test_request_images_refuses_a_file_over_the_cap(hass: HomeAssistant, media: Path) -> None:
    path = media / "before.png"
    with (
        patch("custom_components.mentat.pictures.MAX_SOURCE_FILE_BYTES", 64),
        pytest.raises(HomeAssistantError) as caught,
    ):
        await async_request_images(hass, [str(path)], Dialect.CLEF)
    assert caught.value.translation_key == "image_too_large"
    assert caught.value.translation_placeholders == {
        "source": str(path),
        "size": f"{path.stat().st_size:,} bytes",
        "limit": "64 bytes",
    }


async def test_request_images_stops_reading_a_sizeless_file_at_the_cap(
    hass: HomeAssistant, media: Path
) -> None:
    """A pipe or a device says it holds 0 bytes, so only the read itself can stop."""
    pipe = MagicMock()
    pipe.read.side_effect = lambda n: b"\x01" * n
    pipe.fileno.return_value = 0
    pipe.__enter__.return_value = pipe
    with (
        patch("custom_components.mentat.pictures.MAX_SOURCE_FILE_BYTES", 64),
        patch("custom_components.mentat.pictures.Path.open", return_value=pipe),
        patch("custom_components.mentat.pictures.os.fstat") as fstat,
        pytest.raises(HomeAssistantError) as caught,
    ):
        fstat.return_value.st_size = 0
        await async_request_images(hass, [str(media / "before.png")], Dialect.CLEF)
    pipe.read.assert_called_once_with(65)
    assert caught.value.translation_placeholders["size"] == "65 bytes"


async def test_request_images_counts_before_waking_any_camera(
    hass: HomeAssistant, frame: AsyncMock
) -> None:
    sources = ["camera.a", "camera.b", "camera.c", "camera.d"]
    with pytest.raises(ServiceValidationError) as caught:
        await async_request_images(hass, sources, Dialect.TYPESAFE, max_images=3)
    assert caught.value.translation_key == "too_many_images"
    assert caught.value.translation_placeholders == {"count": "4", "limit": "3"}
    frame.assert_not_awaited()


# Through the actions


async def call(hass: HomeAssistant, action: str, data: dict[str, Any]) -> Any:
    return await hass.services.async_call(DOMAIN, action, data, blocking=True, return_response=True)


def _entry(hass: HomeAssistant, **options: Any) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title="typesafe", data=dict(ENTRY_DATA["typesafe"]), options=options
    )
    entry.add_to_hass(hass)
    return entry


async def test_noul_with_a_camera_sends_it_beside_the_note(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_client: MagicMock, frame: AsyncMock
) -> None:
    hass.states.async_set("camera.driveway", "idle", {"friendly_name": "Driveway"})
    reply = await call(
        hass,
        "noul",
        {
            "pictures": ["camera.driveway"],
            "note": "The car left at eight.",
            "instructions": "Is the car back?",
        },
    )
    assert reply["noul"] == 0.81
    state = fake_client.ask.await_args.args[0]
    sent: Pictures = fake_client.ask.await_args.kwargs["pictures"]
    (reference,) = sent.assets
    assert state == {
        "images": [{"name": "Driveway", "entity_id": "camera.driveway", "image": reference}],
        "note": "The car left at eight.",
    }
    assert sent.tokens == 4704


async def test_noul_with_pictures_and_nothing_else_still_asks(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_client: MagicMock, frame: AsyncMock
) -> None:
    await call(hass, "noul", {"pictures": ["camera.driveway"], "instructions": "Car back?"})
    assert list(fake_client.ask.await_args.args[0]) == ["images"]


async def test_ask_with_a_target_and_pictures_sends_both(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_client: MagicMock, frame: AsyncMock
) -> None:
    hass.states.async_set("binary_sensor.gate", "off")
    fake_client.ask.return_value = build_response(closed=NoulAnswer(noul=0.2))
    await call(
        hass,
        "ask",
        {
            "entity_id": "binary_sensor.gate",
            "pictures": ["camera.driveway"],
            "questions": {"closed": {"type": "noul", "instructions": "Gate shut?"}},
        },
    )
    assert set(fake_client.ask.await_args.args[0]) >= {"now", "entities", "images"}


@pytest.mark.parametrize("provider_key", ["local_clef"], indirect=True)
async def test_noul_on_a_clef_entry_numbers_the_pictures(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_client: MagicMock,
    frame: AsyncMock,
    media: Path,
) -> None:
    fake_client.dialect = Dialect.CLEF
    path = str(media / "before.png")
    await call(
        hass,
        "noul",
        {
            "pictures": [path, "camera.driveway"],
            "instructions": "Has picture 2 a car picture 1 lacks?",
        },
    )
    assert fake_client.ask.await_args.args[0] == {
        "images": [
            {"index": 1, "name": "before.png", "path": path},
            {"index": 2, "name": "camera.driveway", "entity_id": "camera.driveway"},
        ]
    }
    sent: Pictures = fake_client.ask.await_args.kwargs["pictures"]
    assert [p.sent[0] for p in sent.ordered] == [500, 1024]


async def test_noul_over_the_entry_picture_limit_wakes_no_camera(
    hass: HomeAssistant, fake_client: MagicMock, frame: AsyncMock
) -> None:
    entry = _entry(hass, **{CONF_MAX_IMAGES: 2})
    assert await hass.config_entries.async_setup(entry.entry_id)
    with pytest.raises(ServiceValidationError) as caught:
        await call(
            hass, "noul", {"pictures": ["camera.a", "camera.b", "camera.c"], "instructions": "Car?"}
        )
    assert caught.value.translation_placeholders == {"count": "3", "limit": "2"}
    frame.assert_not_awaited()


async def test_noul_estimate_adds_the_picture_tokens(
    hass: HomeAssistant, fake_client: MagicMock, frame: AsyncMock
) -> None:
    """3,000 tokens pay for a text call, not for a frame billed 4,704."""
    entry = _entry(hass, **{CONF_TOKEN_BUDGET: 3_000})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await call(hass, "noul", {"note": "quiet", "instructions": "Car back?"})
    sent = fake_client.ask.await_count
    with pytest.raises(HomeAssistantError) as caught:
        await call(hass, "noul", {"pictures": ["camera.driveway"], "instructions": "Car back?"})
    assert caught.value.translation_key == "request_over_budget"
    assert int(caught.value.translation_placeholders["estimate"]) > 4704
    assert fake_client.ask.await_count == sent


@pytest.mark.parametrize(
    ("action", "data"),
    [
        ("noul", {"pictures": ["camera.driveway"], "instructions": "Car back?"}),
        ("box", {"picture": "camera.driveway", "instructions": "the car"}),
    ],
    ids=["noul", "box"],
)
async def test_spent_budget_wakes_no_camera(
    hass: HomeAssistant,
    fake_client: MagicMock,
    frame: AsyncMock,
    action: str,
    data: dict[str, Any],
) -> None:
    entry = _entry(hass, **{CONF_TOKEN_BUDGET: 150})
    assert await hass.config_entries.async_setup(entry.entry_id)
    with pytest.raises(HomeAssistantError) as caught:
        await call(hass, action, data)
    assert caught.value.translation_key == "request_over_budget"
    assert caught.value.translation_placeholders["remaining"] == str(150 - PROBE_TOKENS)
    frame.assert_not_awaited()
    assert fake_client.ask.await_count == 1
