"""Provider presets, dialects, prices and image arithmetic."""

from __future__ import annotations

import pytest

from custom_components.mentat.providers import (
    PROVIDERS,
    Dialect,
    ImageFormat,
    default_image_format,
    entry_url,
    image_limits,
    image_tokens,
    price_for,
    provider_for,
    resolve_url,
)

from .conftest import ACCOUNT_ID, ENTRY_DATA, LOCAL_URL


def test_every_preset_is_keyed_by_its_own_key() -> None:
    assert set(PROVIDERS) == {
        "typesafe",
        "openrouter",
        "cloudflare",
        "local_clef",
        "generic",
    }
    for key, provider in PROVIDERS.items():
        assert provider.key == key


@pytest.mark.parametrize(
    ("key", "dialect", "image_format"),
    [
        ("typesafe", Dialect.TYPESAFE, ImageFormat.ASSETS),
        ("openrouter", Dialect.TYPESAFE, ImageFormat.ASSETS),
        ("cloudflare", Dialect.CLEF, ImageFormat.BASE64_OBJECT),
        ("local_clef", Dialect.CLEF, ImageFormat.DATA_URL),
        ("generic", Dialect.TYPESAFE, ImageFormat.ASSETS),
    ],
)
def test_provider_for_reads_the_preset(
    key: str, dialect: Dialect, image_format: ImageFormat
) -> None:
    provider = provider_for(ENTRY_DATA[key])
    assert provider.key == key
    assert provider.dialect is dialect
    assert provider.image_format is image_format


def test_a_preset_ignores_a_stored_dialect() -> None:
    """Only the generic provider's dialect is the user's to choose."""
    provider = provider_for({"provider": "typesafe", "dialect": "clef"})
    assert provider.dialect is Dialect.TYPESAFE


def test_generic_takes_dialect_and_encoding_from_the_entry() -> None:
    provider = provider_for({"provider": "generic", "dialect": "clef", "image_format": "base64"})
    assert provider.dialect is Dialect.CLEF
    assert provider.image_format is ImageFormat.BASE64_OBJECT
    # The preset itself is untouched.
    assert PROVIDERS["generic"].dialect is Dialect.TYPESAFE


def test_generic_defaults_the_encoding_to_its_dialect() -> None:
    assert provider_for({"provider": "generic", "dialect": "clef"}).image_format is (
        ImageFormat.DATA_URL
    )
    assert default_image_format(Dialect.TYPESAFE) is ImageFormat.ASSETS


def test_an_unknown_provider_is_generic() -> None:
    assert provider_for({"provider": "gone"}).key == "generic"
    assert provider_for({}).key == "generic"


def test_resolve_url_fills_account_and_model() -> None:
    cloudflare = PROVIDERS["cloudflare"]
    assert resolve_url(cloudflare, None, ACCOUNT_ID, "clef") == (
        f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/run/@cf/cloudflare/clef"
    )


def test_resolve_url_leaves_the_model_template_without_a_model() -> None:
    url = resolve_url(PROVIDERS["cloudflare"], None, ACCOUNT_ID, None)
    assert url.endswith("/@cf/cloudflare/{model}")
    assert "{account_id}" not in url


def test_resolve_url_needs_an_account_id_for_a_template() -> None:
    with pytest.raises(ValueError, match="account id"):
        resolve_url(PROVIDERS["cloudflare"], None, None, "clef")


def test_resolve_url_needs_an_address_for_generic() -> None:
    with pytest.raises(ValueError, match="no address"):
        resolve_url(PROVIDERS["generic"], "  ", None, None)


def test_resolve_url_prefers_the_given_address_and_trims_it() -> None:
    assert resolve_url(PROVIDERS["local_clef"], f" {LOCAL_URL}/ ", None, "x") == (LOCAL_URL)
    assert resolve_url(PROVIDERS["typesafe"], None, None, None) == ("https://api.typesafe.ai")


def test_entry_url_resolves_with_the_model() -> None:
    assert entry_url(ENTRY_DATA["cloudflare"]).endswith("/clef-flash")
    assert entry_url({"provider": "local_clef"}) == "http://127.0.0.1:8000"


@pytest.mark.parametrize(
    ("key", "model", "price"),
    [
        ("typesafe", "jev-latest", 0.042),
        ("openrouter", "~typesafe/jev-latest", 0.042),
        ("cloudflare", "clef", 0.24),
        ("cloudflare", "clef-flash", 0.038),
        ("cloudflare", "something-else", 0.038),
        ("local_clef", "clef-flash", 0.0),
        ("generic", "jev-latest", 0.0),
        ("unknown", "x", 0.0),
    ],
)
def test_price_for(key: str, model: str, price: float) -> None:
    assert price_for(key, model) == price


def test_image_limits_per_dialect() -> None:
    typesafe = image_limits(Dialect.TYPESAFE)
    assert (typesafe.min_side, typesafe.max_side) == (280, 1568)
    assert typesafe.max_bytes == 16 * 1024 * 1024
    assert typesafe.max_pixels is None
    clef = image_limits(Dialect.CLEF)
    assert clef.max_side == 1024
    assert clef.max_pixels == 1024 * 1024
    assert clef.max_bytes == 4 * 1024 * 1024
    assert clef.max_source_pixels == 16_000_000


@pytest.mark.parametrize(
    ("width", "height", "tokens"),
    # Billed amounts measured against jev-1.14.0 on 2026-10-16.
    [(280, 280, 200), (300, 301, 242), (784, 588, 1176), (1568, 1176, 4704)],
)
def test_typesafe_image_tokens_match_measured_bills(width: int, height: int, tokens: int) -> None:
    assert image_tokens(Dialect.TYPESAFE, width, height) == tokens


def test_clef_image_tokens_are_patches_plus_framing_capped() -> None:
    assert image_tokens(Dialect.CLEF, 32, 32) == 4
    assert image_tokens(Dialect.CLEF, 640, 480) == 20 * 15 + 3
    assert image_tokens(Dialect.CLEF, 4096, 4096) == 1024
