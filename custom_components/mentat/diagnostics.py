"""Diagnostics. Users paste these into issues, so no credential ever appears.

The report holds the entry with its secrets redacted, which provider, dialect
and model it talks to, today's ledger, and counts for every window. A window's
log itself is the house's comings and goings, so only its sizes leave.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import CONF_ACCOUNT_ID

if TYPE_CHECKING:
    from datetime import datetime

    from . import MentatConfigEntry
    from .budget import Ledger
    from .coordinator import MentatCoordinator
    from .window import EventWindow

# async_redact_data matches keys exactly, so every spelling that can appear is
# listed. The account id is not a secret on its own, but it names the account a
# leaked token would spend.
TO_REDACT: Final = {
    CONF_API_KEY,
    CONF_ACCOUNT_ID,
    "apikey",
    "authorization",
    "key",
    "token",
    "access_token",
}
# A picture of a person, and where the house is.
PRIVATE: Final = {"entity_picture", "latitude", "longitude", "gps_accuracy"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: MentatConfigEntry
) -> dict[str, Any]:
    runtime = entry.runtime_data
    provider = runtime.provider
    now = dt_util.utcnow()
    coordinators = dict(sorted(runtime.coordinators.items()))
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT | PRIVATE),
            "options": dict(entry.options),
        },
        "provider": provider.key,
        "dialect": provider.dialect.value,
        "image_format": provider.image_format.value,
        "model": runtime.model,
        "model_version": runtime.model_version,
        "ledger_today": _ledger_today(runtime.ledger),
        "watches": list(coordinators),
        "windows": {
            key: _window_counts(coordinator, coordinator.window, now)
            for key, coordinator in coordinators.items()
            if coordinator.window is not None
        },
    }


def _ledger_today(ledger: Ledger) -> dict[str, Any]:
    return {
        "day": ledger.day.isoformat(),
        "requests": ledger.requests,
        "tokens": ledger.tokens,
        "budget": ledger.budget,
        "over_budget": ledger.over_budget,
        "bytes_per_token": round(ledger.bytes_per_token, 3),
        "spend_usd": round(ledger.spend, 6),
    }


def _window_counts(
    coordinator: MentatCoordinator, window: EventWindow, now: datetime
) -> dict[str, Any]:
    """How a window is set up and how full it is. Counts only."""
    config = window.config
    span = coordinator.window_span
    return {
        "evaluate": config.evaluate,
        "minutes": round(config.duration.total_seconds() / 60),
        "max_events": config.max_events,
        "event_filters": len(config.events),
        "entities": len(window.entity_ids),
        "buffered": window.buffered,
        "in_window": len(window.entries(now)),
        "truncated": window.truncated(now),
        "last_evaluated_events": 0 if span is None else span.events,
    }
