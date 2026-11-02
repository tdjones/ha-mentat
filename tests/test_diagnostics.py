"""Diagnostics and system health. Neither may carry a credential."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    get_system_health_info,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.mentat.const import CONF_ACCOUNT_ID, DOMAIN
from custom_components.mentat.diagnostics import (
    PRIVATE,
    TO_REDACT,
    async_get_config_entry_diagnostics,
)
from custom_components.mentat.providers import Dialect, ImageFormat

from .conftest import ACCOUNT_ID, API_KEY, PROBE_TOKENS

CLOUDFLARE_PRICE = 0.038


@pytest.mark.parametrize("provider_key", ["cloudflare"], indirect=True)
async def test_diagnostics_hide_the_key_and_the_account(
    hass: HomeAssistant, fake_client: MagicMock, loaded: MockConfigEntry
) -> None:
    loaded.runtime_data.coordinators["porch"] = MagicMock(window=None)
    report = await async_get_config_entry_diagnostics(hass, loaded)

    text = repr(report)
    assert API_KEY not in text
    assert ACCOUNT_ID not in text
    data = report["entry"]["data"]
    assert data[CONF_API_KEY] == data[CONF_ACCOUNT_ID] == "**REDACTED**"
    assert report["provider"] == "cloudflare"
    assert report["dialect"] == Dialect.CLEF
    assert report["image_format"] == ImageFormat.BASE64_OBJECT
    assert report["model"] == "clef-flash"
    today = report["ledger_today"]
    assert (today["requests"], today["tokens"], today["over_budget"]) == (1, PROBE_TOKENS, False)
    assert today["spend_usd"] == round(PROBE_TOKENS / 1_000_000 * CLOUDFLARE_PRICE, 6)
    assert report["watches"] == ["porch"]
    assert report["windows"] == {}


def test_diagnostics_private_keys_name_pictures_and_places() -> None:
    assert {"latitude", "longitude", "gps_accuracy", "entity_picture"} == PRIVATE
    assert not PRIVATE & TO_REDACT


async def test_diagnostics_health_without_entries_counts_none(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "system_health", {})
    assert await async_setup_component(hass, DOMAIN, {})
    assert await get_system_health_info(hass, DOMAIN) == {"loaded_entries": 0}


@pytest.mark.parametrize("provider_key", ["cloudflare"], indirect=True)
async def test_diagnostics_health_checks_only_the_origin(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    fake_client: MagicMock,
    loaded: MockConfigEntry,
) -> None:
    aioclient_mock.get("https://api.cloudflare.com", status=403)
    assert await async_setup_component(hass, "system_health", {})
    info = await get_system_health_info(hass, DOMAIN)
    # The panel awaits the check itself.
    info["reachable"] = await info["reachable"]
    assert info == {
        "loaded_entries": 1,
        "provider": "Cloudflare Workers AI",
        "dialect": "clef",
        "reachable": "ok",
    }
    # The account id is in the request path, so the check never goes there.
    assert [str(call[1]) for call in aioclient_mock.mock_calls] == ["https://api.cloudflare.com"]
