"""Every picture a request carries: fetched, turned upright, sized, re-encoded and billed.

A source is a camera or image entity, or the absolute path of a file Home
Assistant may read. Pictures are handled in the order the caller gave them, and
the first one that fails stops the request before anything is sent. Whatever a
picture arrived as, it leaves as a baseline JPEG at the size its dialect takes,
and that size is also the one it is billed at. TypeSafe refuses a side outside
280 to 1568 pixels and never resizes, and a 1920 x 1080 camera frame is outside
it. Clef bills at most 1024 tokens a picture, which 1024 px already reaches, so
anything larger costs upload time and buys nothing.

This module is the only place that knows how a dialect points at a picture from
the state. TypeSafe finds an asset by a reference string written in the state.
Clef sends the pictures as a list before the state, so the state can only count
them. It refuses anything that is not a picture source and never talks to the
model itself.
"""

from __future__ import annotations

import asyncio
import io
import os
import struct
import uuid
from collections.abc import Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from homeassistant.components import camera, image
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, split_entity_id, valid_entity_id
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from jevclient import Image
from PIL import Image as PILImage
from PIL import ImageFile, ImageOps, UnidentifiedImageError

from .const import (
    DEFAULT_MAX_IMAGES,
    DOMAIN,
    IMAGE_JPEG_QUALITY,
    MAX_SOURCE_FILE_BYTES,
    SNAPSHOT_TIMEOUT_SECONDS,
)
from .providers import Dialect, ImageLimits, image_limits, image_tokens

IMAGE_DOMAINS: tuple[str, ...] = (Platform.CAMERA, Platform.IMAGE)
# Every picture leaves as a JPEG, so this is the only type a dialect has to name.
MIME_TYPE: Final = "image/jpeg"
# The key the state lists pictures under. The model reads it, so it stays.
STATE_KEY: Final = "images"
# Each pass that has to shrink an image to fit the byte cap takes a quarter off
# both sides, which roughly halves the encoded size.
_SHRINK = 0.75


@dataclass(frozen=True, slots=True)
class Picture:
    """One picture ready to send.

    `sent` is the width and height it goes out at, which is what it is billed
    at. `source` is the upright size it had before resizing, which is what a box
    edge in pixels is measured against.
    """

    payload: Image
    sent: tuple[int, int]
    source: tuple[int, int]


@dataclass(slots=True)
class Pictures:
    """The pictures of one request, kept in the shape its dialect sends them.

    `ordered` holds them in the order given, which is the order Clef posts them
    in. `assets` is TypeSafe's map from reference to picture, and stays empty for
    Clef. `entries` is what the state says about each one, and `tokens` what
    they cost together.
    """

    dialect: Dialect
    entries: list[dict[str, Any]] = field(default_factory=list)
    assets: dict[str, Image] = field(default_factory=dict)
    ordered: list[Picture] = field(default_factory=list)
    tokens: int = 0

    @classmethod
    def empty(cls, dialect: Dialect) -> Pictures:
        """No pictures yet, for a request in this dialect."""
        return cls(dialect)

    def add(self, label: Mapping[str, Any], picture: Picture) -> None:
        """Take one more picture, named in the state by `label`."""
        self.ordered.append(picture)
        if self.dialect is Dialect.CLEF:
            # Posted ahead of the state, so the state can only say which picture
            # is which by its place in the list, counted from one.
            self.entries.append({"index": len(self.ordered)} | dict(label))
        else:
            # The API looks for a reference as plain text in every string of the
            # state, so a fresh UUID is the one thing that cannot turn up inside
            # a sentence by accident.
            reference = str(uuid.uuid4())
            self.assets[reference] = picture.payload
            self.entries.append(dict(label) | {"image": reference})
        self.tokens += image_tokens(self.dialect, *picture.sent)

    def __bool__(self) -> bool:
        return len(self.ordered) > 0


def attach(state: Any, pictures: Pictures | None) -> Any:
    """The state with the pictures listed in it.

    A mapping state gains an `images` key. Text or a list becomes the `note` of
    a new mapping, the key readings.async_state uses for text beside entities,
    so a question finds one shape whatever the call carried.
    """
    if pictures is None or not pictures:
        return state
    listed = {STATE_KEY: pictures.entries}
    if isinstance(state, dict):
        return state | listed
    if state is None or state == "":
        return listed
    return listed | {"note": state}


def label_for_entity(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """How the state names a camera's picture, so an instruction can say "the driveway".

    An entity with no state is named by its id.
    """
    current = hass.states.get(entity_id)
    return {"name": entity_id if current is None else current.name, "entity_id": entity_id}


def label_for_path(path: str) -> dict[str, Any]:
    """How the state names a file's picture: its file name, and the full path."""
    return {"name": Path(path).name, "path": path}


def _unreadable(source: str, reason: object) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="image_unreadable",
        translation_placeholders={"source": source, "reason": str(reason)},
    )


def _too_large(source: str, size: str, limit: str) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="image_too_large",
        translation_placeholders={"source": source, "size": size, "limit": limit},
    )


def _unavailable(entity_id: str, reason: object) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="image_unavailable",
        translation_placeholders={"entity_id": entity_id, "reason": str(reason)},
    )


# What Pillow raises on a picture it cannot read. A corrupt EXIF block can raise
# SyntaxError, struct.error or TypeError from getexif() or exif_transpose()
# rather than one of Pillow's own errors (Pillow 12, found by fuzzing the IFD).
_UNREADABLE: tuple[type[Exception], ...] = (
    UnidentifiedImageError,
    OSError,
    ValueError,
    SyntaxError,
    TypeError,
    struct.error,
)

# EXIF orientations that turn the picture a quarter, so its sides swap.
_QUARTER_TURNS: frozenset[int] = frozenset({5, 6, 7, 8})
_ORIENTATION_TAG = 0x0112


def _open(data: bytes, source: str, limits: ImageLimits) -> ImageFile.ImageFile:
    """The image with its header read and its pixels not, refused if too large.

    Pillow raises past twice MAX_IMAGE_PIXELS (about 179 MP) on open. Below that
    it only warns past MAX_IMAGE_PIXELS, and every dialect's max_source_pixels is
    under that, so the size check below refuses those pictures first. The
    warning is left alone: silencing it would mean changing the process-wide
    warning filter from an executor thread.
    """
    try:
        opened = PILImage.open(io.BytesIO(data))
    except PILImage.DecompressionBombError:
        raise _too_large(
            source,
            f"more than {2 * (PILImage.MAX_IMAGE_PIXELS or 0):,} pixels",
            f"{limits.max_source_pixels:,} pixels",
        ) from None
    width, height = opened.size
    if width * height > limits.max_source_pixels:
        opened.close()
        raise _too_large(
            source, f"{width} x {height} pixels", f"{limits.max_source_pixels:,} pixels"
        )
    return opened


def prepare_image(data: bytes, source: str, limits: ImageLimits) -> Picture:
    """Turn, flatten, scale and re-encode one image. Blocking: use the executor.

    Neither API applies EXIF orientation, so a phone photo that looks upright
    would reach the model on its side. Everything goes out as a baseline JPEG,
    the one format both dialects read and the kind TypeSafe's docs prepare.
    """
    try:
        with _open(data, source, limits) as opened:
            width, height = opened.size
            if opened.getexif().get(_ORIENTATION_TAG) in _QUARTER_TURNS:
                source_width, source_height = height, width
            else:
                source_width, source_height = width, height
            # A JPEG decodes at a half, a quarter or an eighth of its size for a
            # fraction of the memory and time, never below what is asked for. A
            # 48 MP photo sent at 1024 px is decoded at 2016 px. Other formats
            # ignore this.
            opened.draft(None, _fit((width, height), limits))
            picture = ImageOps.exif_transpose(opened)
            picture.load()
    except _UNREADABLE as err:
        raise _unreadable(source, err) from err

    flat = _opaque(picture)
    size = _fit(flat.size, limits)
    while True:
        sized = flat if size == flat.size else flat.resize(size, PILImage.Resampling.LANCZOS)
        encoded = _encode(sized)
        if len(encoded) <= limits.max_bytes:
            break
        smaller = (
            max(limits.min_side, round(size[0] * _SHRINK)),
            max(limits.min_side, round(size[1] * _SHRINK)),
        )
        if smaller == size:
            raise _too_large(source, f"{len(encoded):,} bytes", f"{limits.max_bytes:,} bytes")
        size = smaller
    return Picture(payload=Image(encoded, "jpg"), sent=size, source=(source_width, source_height))


def _opaque(image: PILImage.Image) -> PILImage.Image:
    """The picture laid over white, as RGB.

    JPEG has no alpha channel. Converting a transparent PNG straight to RGB
    keeps whatever colour sits under the alpha, which is usually black.
    """
    if not image.has_transparency_data:
        return image.convert("RGB")
    layer = image.convert("RGBA")
    white = PILImage.new("RGBA", layer.size, (255, 255, 255, 255))
    return PILImage.alpha_composite(white, layer).convert("RGB")


def _fit(size: tuple[int, int], limits: ImageLimits) -> tuple[int, int]:
    """The size to send, keeping the aspect ratio where the limits allow it.

    A larger image comes down to the longest side (and pixel count) the dialect
    takes. A side under the minimum goes up to it. When both cannot hold, which
    for TypeSafe takes an image 5.6 times as wide as it is tall, the long side is
    fitted and the short one stretched. A box comes back in fractions of the
    picture as sent, and stretching keeps them the same fractions of the source,
    where padding would shift them.
    """
    width, height = size
    longest, shortest = max(width, height), min(width, height)
    scale = min(1.0, limits.max_side / longest)
    if limits.max_pixels is not None:
        scale = min(scale, (limits.max_pixels / (width * height)) ** 0.5)
    if shortest * scale < limits.min_side:
        scale = min(limits.min_side / shortest, limits.max_side / longest)
    return (
        min(limits.max_side, max(limits.min_side, round(width * scale))),
        min(limits.max_side, max(limits.min_side, round(height * scale))),
    )


def _encode(picture: PILImage.Image) -> bytes:
    buffer = io.BytesIO()
    picture.save(buffer, "JPEG", quality=IMAGE_JPEG_QUALITY, progressive=False)
    return buffer.getvalue()


class _FileTooLargeError(Exception):
    def __init__(self, size: int) -> None:
        super().__init__(size)
        self.size = size


def _read_file(path: str | Path) -> bytes:
    """A file's bytes, refusing one too large to be a still. Blocking.

    At most one byte past the cap is read, whatever the file claims to be: a
    device or a pipe has no size to check first.
    """
    with Path(path).open("rb") as handle:
        data = handle.read(MAX_SOURCE_FILE_BYTES + 1)
        if len(data) > MAX_SOURCE_FILE_BYTES:
            raise _FileTooLargeError(max(len(data), os.fstat(handle.fileno()).st_size))
    return data


async def async_read_file(hass: HomeAssistant, path: str | Path, source: str) -> bytes:
    """A picture file's bytes, read in the executor and capped in size.

    `source` is what an error names: the path for a file source, the media id
    for an AI Task attachment.
    """
    try:
        return await hass.async_add_executor_job(_read_file, path)
    except _FileTooLargeError as err:
        raise _too_large(source, f"{err.size:,} bytes", f"{MAX_SOURCE_FILE_BYTES:,} bytes") from err
    except (OSError, ValueError) as err:
        raise _unreadable(source, err) from err


async def _async_entity_bytes(hass: HomeAssistant, entity_id: str) -> bytes:
    """The latest still of a camera or image entity.

    The platform module is picked per call rather than kept in a table, so a
    test that patches camera.async_get_image reaches this code.
    """
    platform = camera if split_entity_id(entity_id)[0] == Platform.CAMERA else image
    try:
        still = await platform.async_get_image(hass, entity_id, timeout=SNAPSHOT_TIMEOUT_SECONDS)
    except HomeAssistantError as err:
        raise _unavailable(entity_id, err) from err
    return bytes(still.content)


async def _async_file_bytes(hass: HomeAssistant, path: str) -> bytes:
    allowed = await hass.async_add_executor_job(hass.config.is_allowed_path, path)
    if not allowed:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="path_not_allowed",
            translation_placeholders={"path": path},
        )
    return await async_read_file(hass, path, path)


def is_path_source(source: str) -> bool:
    """Whether a source names a file. Only a full path does."""
    return Path(source).is_absolute()


def is_entity_source(source: str) -> bool:
    """Whether a source names a camera or image entity."""
    return valid_entity_id(source) and split_entity_id(source)[0] in IMAGE_DOMAINS


async def async_fetch_source(hass: HomeAssistant, source: str) -> tuple[bytes, dict[str, Any]]:
    """The bytes behind one source, and the label the state gives them."""
    if is_path_source(source):
        return await _async_file_bytes(hass, source), label_for_path(source)
    if is_entity_source(source):
        return await _async_entity_bytes(hass, source), label_for_entity(hass, source)
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="not_an_image_source",
        translation_placeholders={"source": source},
    )


async def async_prepare(hass: HomeAssistant, data: bytes, source: str, dialect: Dialect) -> Picture:
    """prepare_image() in the executor, sized for this dialect."""
    return await hass.async_add_executor_job(prepare_image, data, source, image_limits(dialect))


def check_image_count(count: int, max_images: int) -> None:
    """Refuse more images than the entry allows, before any is fetched."""
    if count > max_images:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="too_many_images",
            translation_placeholders={"count": str(count), "limit": str(max_images)},
        )


# One picture still to fetch: what an error calls it, and the coroutine that
# returns its bytes and the label the state gives them.
type Fetch = tuple[str, Coroutine[Any, Any, tuple[bytes, Mapping[str, Any]]]]


async def async_gather_pictures(
    hass: HomeAssistant, fetches: Sequence[Fetch], dialect: Dialect
) -> Pictures:
    """Fetch and prepare every picture, and keep them in the order given.

    The fetches run together: three cameras that take a second each should not
    take three. So does the resizing, each in its own executor job. The first
    failure is the one reported, and nothing is sent.
    """
    fetched = await asyncio.gather(*(fetch for _source, fetch in fetches))
    prepared = await asyncio.gather(
        *(
            async_prepare(hass, data, source, dialect)
            for (source, _fetch), (data, _label) in zip(fetches, fetched, strict=True)
        )
    )
    pictures = Pictures.empty(dialect)
    for (_data, label), picture in zip(fetched, prepared, strict=True):
        pictures.add(label, picture)
    return pictures


async def async_request_images(
    hass: HomeAssistant,
    sources: Sequence[str],
    dialect: Dialect,
    *,
    max_images: int = DEFAULT_MAX_IMAGES,
) -> Pictures:
    """Every source of an action as a picture, refused before any fetch if too many."""
    check_image_count(len(sources), max_images)
    fetches = [(source, async_fetch_source(hass, source)) for source in sources]
    return await async_gather_pictures(hass, fetches, dialect)
