"""System health: which server an entry talks to, and whether it can be reached."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components import system_health
from homeassistant.core import HomeAssistant, callback
from yarl import URL

from .const import DOMAIN
from .providers import entry_url, provider_for


@callback
def async_register(hass: HomeAssistant, register: system_health.SystemHealthRegistration) -> None:
    register.async_register_info(system_health_info)


async def system_health_info(hass: HomeAssistant) -> dict[str, Any]:
    """How many entries are loaded, and the first one's server. The panel holds one answer."""
    loaded = hass.config_entries.async_loaded_entries(DOMAIN)
    info: dict[str, Any] = {"loaded_entries": len(loaded)}
    if loaded:
        info.update(_server(hass, loaded[0].data))
    return info


def _server(hass: HomeAssistant, data: Mapping[str, Any]) -> dict[str, Any]:
    """The provider, its dialect, and a reachability check of the server's origin.

    The check fetches the origin rather than the request path. A GET on the
    request path is an error page at best, any answer at all proves the host
    is reachable, and a Cloudflare path would carry the account id.
    """
    provider = provider_for(data)
    origin = URL(entry_url(data)).origin()
    return {
        "provider": provider.title,
        "dialect": provider.dialect.value,
        "reachable": system_health.async_check_can_reach_url(hass, str(origin)),
    }
