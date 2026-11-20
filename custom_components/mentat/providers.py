"""Who answers, which wire dialect they speak, and what they charge.

A provider is a preset: an address, a default model, a price and the dialect the
server speaks. The generic provider has none of its own, so its entry names the
dialect and the image encoding, and provider_for() folds those in.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from math import ceil
from typing import Any, Final

from homeassistant.const import CONF_MODEL, CONF_URL

from .const import CONF_ACCOUNT_ID, CONF_DIALECT, CONF_IMAGE_FORMAT, CONF_PROVIDER


class Dialect(StrEnum):
    """The shape of the request and the reply.

    TYPESAFE is the published /v1/systemone API: images are named assets that the
    state refers to by a reference string. CLEF sends images as an ordered list
    before the state and may wrap the reply in Cloudflare's envelope.
    """

    TYPESAFE = "typesafe"
    CLEF = "clef"


class ImageFormat(StrEnum):
    """How an image travels in the body."""

    # {"assets": {ref: {"type": "image", "format": "jpg", "data_base64": ...}}}
    ASSETS = "assets"
    # "images": ["data:image/jpeg;base64,..."], what clef_mlx.py reads.
    DATA_URL = "data_url"
    # "images": [{"content_type": "image/jpeg", "base64": ...}], Workers AI's form.
    BASE64_OBJECT = "base64"


@dataclass(frozen=True, slots=True)
class Provider:
    key: str
    title: str
    # None for a server that has no address of its own. May carry {account_id} and
    # {model}, in which case it is the whole request URL rather than a base.
    base_url: str | None
    model: str
    key_required: bool
    usd_per_million_input_tokens: float
    dialect: Dialect
    image_format: ImageFormat
    needs_account_id: bool = False
    # Model ids offered in a select. Empty means free text.
    models: tuple[str, ...] = ()


PROVIDER_TYPESAFE: Final = "typesafe"
PROVIDER_OPENROUTER: Final = "openrouter"
PROVIDER_CLOUDFLARE: Final = "cloudflare"
PROVIDER_LOCAL_CLEF: Final = "local_clef"
PROVIDER_GENERIC: Final = "generic"

# Prices are US dollars per million input tokens; output is not billed. TypeSafe's
# is jevclient's USD_PER_MILLION_INPUT_TOKENS, and OpenRouter passes it through.
# Cloudflare's are from the Workers AI pricing page, read 2026-10-30.
PROVIDERS: Final[dict[str, Provider]] = {
    PROVIDER_TYPESAFE: Provider(
        key=PROVIDER_TYPESAFE,
        title="TypeSafe Jev",
        base_url="https://api.typesafe.ai",
        model="jev-latest",
        key_required=True,
        usd_per_million_input_tokens=0.042,
        dialect=Dialect.TYPESAFE,
        image_format=ImageFormat.ASSETS,
        models=("jev-latest", "jev-preview", "jev-1.14.0", "jev-1.13.0"),
    ),
    PROVIDER_OPENROUTER: Provider(
        key=PROVIDER_OPENROUTER,
        title="OpenRouter",
        base_url="https://openrouter.ai/api",
        model="~typesafe/jev-latest",
        key_required=True,
        usd_per_million_input_tokens=0.042,
        dialect=Dialect.TYPESAFE,
        image_format=ImageFormat.ASSETS,
    ),
    PROVIDER_CLOUDFLARE: Provider(
        key=PROVIDER_CLOUDFLARE,
        title="Cloudflare Workers AI",
        base_url=(
            "https://api.cloudflare.com/client/v4/accounts/{account_id}"
            "/ai/run/@cf/cloudflare/{model}"
        ),
        model="clef-flash",
        key_required=True,
        usd_per_million_input_tokens=0.038,
        dialect=Dialect.CLEF,
        image_format=ImageFormat.BASE64_OBJECT,
        needs_account_id=True,
        models=("clef-flash", "clef"),
    ),
    PROVIDER_LOCAL_CLEF: Provider(
        key=PROVIDER_LOCAL_CLEF,
        title="Local Clef server",
        base_url="http://127.0.0.1:8000",
        model="clef-flash",
        key_required=False,
        usd_per_million_input_tokens=0.0,
        dialect=Dialect.CLEF,
        image_format=ImageFormat.DATA_URL,
    ),
    PROVIDER_GENERIC: Provider(
        key=PROVIDER_GENERIC,
        title="System One server",
        base_url=None,
        model="jev-latest",
        key_required=False,
        usd_per_million_input_tokens=0.0,
        dialect=Dialect.TYPESAFE,
        image_format=ImageFormat.ASSETS,
    ),
}

# Cloudflare bills its two models differently (pricing page, read 2026-10-30).
_CLOUDFLARE_PRICES: Final = {"clef": 0.24, "clef-flash": 0.038}


def default_image_format(dialect: Dialect) -> ImageFormat:
    """The encoding a server of this dialect reads when nothing else is said."""
    return ImageFormat.ASSETS if dialect is Dialect.TYPESAFE else ImageFormat.DATA_URL


def provider_for(entry_data: Mapping[str, Any]) -> Provider:
    """The provider an entry talks to, with the entry's own dialect folded in.

    The presets are authoritative for their dialect. Only the generic provider,
    which is whatever server the user named, takes the dialect and the image
    encoding from the entry. An unknown or missing provider is treated as generic,
    so a stored entry never fails to load over a renamed preset.
    """
    provider = PROVIDERS.get(entry_data.get(CONF_PROVIDER) or "")
    if provider is None:
        provider = PROVIDERS[PROVIDER_GENERIC]
    if provider.key != PROVIDER_GENERIC:
        return provider
    dialect = Dialect(entry_data.get(CONF_DIALECT) or provider.dialect)
    image_format = ImageFormat(entry_data.get(CONF_IMAGE_FORMAT) or default_image_format(dialect))
    return replace(provider, dialect=dialect, image_format=image_format)


def model_offered(provider: Provider, model: str) -> bool:
    """Whether a request to this provider may name this model.

    A model id has no spaces. Where the address carries {model}, the id becomes
    part of the request path, so it must be one the provider lists: Workers AI
    publishes exactly those, and anything else is a typo or, with `..` in it, a
    path to some other endpoint that the account's key would be sent to.
    """
    if not model or any(character.isspace() for character in model):
        return False
    return "{model}" not in (provider.base_url or "") or model in provider.models


def resolve_url(
    provider: Provider, url: str | None, account_id: str | None, model: str | None
) -> str:
    """The address a request goes to, with the templates filled in.

    `url` overrides the provider's address. {account_id} is always filled, and a
    template that needs one without being given one is a ValueError. {model} is
    filled only when `model` is given: the unique id is built with model=None, so
    switching between Clef and Clef-flash keeps the entry's unique id.
    """
    template = (url or provider.base_url or "").strip().rstrip("/")
    if not template:
        raise ValueError(f"{provider.title} has no address of its own; give one")
    if "{account_id}" in template:
        if not account_id:
            raise ValueError("this address needs a Cloudflare account id")
        template = template.replace("{account_id}", account_id)
    if model is not None:
        template = template.replace("{model}", model)
    return template


def entry_unique_id(resolved_url: str, api_key: str) -> str:
    """The unique id of an entry that sends this key to this resolved address.

    The id lands in the registry in plain text and the key is a credential, so
    the id is the first 16 hex digits of a SHA-256 over both. The address takes
    part because a key is good only where it was issued, and because every
    keyless server would otherwise get the same id. Pass resolve_url() called
    with model=None, so one Cloudflare account is one entry whichever Clef
    model it asks.
    """
    digest = hashlib.sha256()
    for part in (resolved_url, "\n", api_key):
        digest.update(part.encode())
    return digest.hexdigest()[:16]


def entry_url(entry_data: Mapping[str, Any]) -> str:
    """The resolved address an entry talks to, model filled in."""
    provider = provider_for(entry_data)
    return resolve_url(
        provider,
        entry_data.get(CONF_URL),
        entry_data.get(CONF_ACCOUNT_ID),
        entry_data.get(CONF_MODEL) or provider.model,
    )


def price_for(key: str, model: str) -> float:
    """US dollars per million input tokens for this provider and model."""
    if key == PROVIDER_CLOUDFLARE and model in _CLOUDFLARE_PRICES:
        return _CLOUDFLARE_PRICES[model]
    provider = PROVIDERS.get(key, PROVIDERS[PROVIDER_GENERIC])
    return provider.usd_per_million_input_tokens


@dataclass(frozen=True, slots=True)
class ImageLimits:
    """What an endpoint accepts, and what images are resized to before sending."""

    # Shortest side the endpoint accepts. Smaller images are scaled up.
    min_side: int
    # Longest side after resizing.
    max_side: int
    # Total pixels after resizing, or None for no limit beyond the sides.
    max_pixels: int | None
    # Encoded size the endpoint accepts.
    max_bytes: int
    # A source larger than this is refused before it is decoded.
    max_source_pixels: int


_TYPESAFE_LIMITS: Final = ImageLimits(
    # jevclient 1.3.1's MIN_IMAGE_SIDE, MAX_IMAGE_SIDE and MAX_IMAGE_BYTES, which
    # it takes from docs.typesafe.ai/images. The library notes that the API refuses
    # an image outside them rather than resizing it.
    min_side=280,
    max_side=1568,
    max_pixels=None,
    max_bytes=16 * 1024 * 1024,
    # Chosen, not measured: a 48 MP phone photo fits, and a decoded 50 MP RGB
    # picture is 150 MB, the most one request should hold in memory.
    max_source_pixels=50_000_000,
)
_CLEF_LIMITS: Final = ImageLimits(
    # From the planning notes of 2026-10-30: about one megapixel with a 1024 px
    # longest side, 4 MiB per image, sources up to 16 MP. clef-flash took a
    # 1024 x 768 picture and billed what image_tokens() says (docs/measurements.md,
    # pictures). The byte and source caps are not measured. Past one megapixel the
    # 1024 token cap means more pixels buy nothing.
    min_side=32,
    max_side=1024,
    max_pixels=1024 * 1024,
    max_bytes=4 * 1024 * 1024,
    max_source_pixels=16_000_000,
)


def image_limits(dialect: Dialect) -> ImageLimits:
    return _TYPESAFE_LIMITS if dialect is Dialect.TYPESAFE else _CLEF_LIMITS


def image_tokens(dialect: Dialect, width: int, height: int) -> int:
    """Input tokens one image of this size is billed.

    TypeSafe: two tokens per 28 px patch, billed within 2 tokens of that
    for 280 x 280 and 1024 x 768 on jev-1.14.0, 2026-11-01 (docs/measurements.md,
    pictures). Clef: one token per 32 px patch plus three, capped at
    1024. clef-flash billed exactly that for 280 x 280 and 1024 x 768 on 2026-11-01
    (docs/measurements.md, pictures).
    """
    if dialect is Dialect.TYPESAFE:
        return 2 * ceil(width / 28) * ceil(height / 28)
    return min(1024, ceil(width / 32) * ceil(height / 32) + 3)
