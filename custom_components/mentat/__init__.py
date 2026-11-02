"""Ask a System One decision model typed questions about the house.

One config entry is one endpoint, one key and one dialect. Setup proves the three
work with a single probe question before anything else is built. A watch is one
request: every question attached to it is judged against the same state, so
questions batch almost for free in time, though their text is billed as input.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_API_KEY, CONF_MODEL, CONF_URL, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey
from jevclient import JevAuthError, JevError, Noul

from .budget import Ledger, over_budget_issue_id
from .client import build_client
from .const import (
    CONF_ACCOUNT_ID,
    CONF_EXTRA,
    CONF_TOKEN_BUDGET,
    CONF_TOKEN_PRICE,
    DOMAIN,
    PROBE_INSTRUCTIONS,
    PROBE_KEY,
    PROBE_STATE,
    STORAGE_VERSION,
    YAML_ID_SCOPE,
)
from .coordinator import MentatCoordinator, MentatRuntimeData
from .providers import price_for, provider_for
from .schema import CONFIG_SCHEMA as CONFIG_SCHEMA
from .services import async_register_services
from .watches import WatchConfig, watch_from_yaml, watches_from_subentries

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.AI_TASK]

# States an entry can be left in after its setup failed.
_FAILED: Final = frozenset(
    {
        ConfigEntryState.SETUP_ERROR,
        ConfigEntryState.SETUP_RETRY,
        ConfigEntryState.MIGRATION_ERROR,
        ConfigEntryState.FAILED_UNLOAD,
    }
)


@dataclass(slots=True)
class YamlWatches:
    """The `mentat:` list from configuration.yaml as the schema left it, and who runs it."""

    raw: list[ConfigType]
    # The entry running the YAML watches. None until one has taken them.
    holder: str | None = None


YAML_WATCHES: HassKey[YamlWatches] = HassKey(f"{DOMAIN}_yaml")

type MentatConfigEntry = ConfigEntry[MentatRuntimeData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the actions, and hold the YAML watches until their entry loads."""
    async_register_services(hass)
    hass.data[YAML_WATCHES] = YamlWatches(list(config.get(DOMAIN, ())))
    return True


def _first_in_line(hass: HomeAssistant) -> ConfigEntry | None:
    """The oldest enabled entry whose setup has not failed."""
    for candidate in hass.config_entries.async_entries(DOMAIN, include_disabled=False):
        if candidate.state not in _FAILED:
            return candidate
    return None


def _yaml_watches(hass: HomeAssistant, entry: MentatConfigEntry) -> list[WatchConfig]:
    """The YAML watches if this entry runs them, else none.

    YAML names no entry. Handed to every entry, each watch would be asked and
    billed once per entry, so one entry runs them all. The entry that runs them
    keeps them through its reloads. When no entry does, the oldest enabled
    entry that has not failed takes them.
    """
    yaml = hass.data[YAML_WATCHES]
    if not yaml.raw:
        return []
    if yaml.holder is None:
        first = _first_in_line(hass)
        if first is None or first.entry_id != entry.entry_id:
            return []
        yaml.holder = entry.entry_id
    elif yaml.holder != entry.entry_id:
        return []
    return [watch_from_yaml(hass, raw) for raw in yaml.raw]


@callback
def _pass_on_yaml(hass: HomeAssistant, leaving: ConfigEntry) -> None:
    """Give the YAML watches to the next entry when the one meant to run them cannot.

    `leaving` failed to set up, was disabled or was removed. If it ran the YAML
    watches, or was about to take them, the oldest other enabled entry that is
    loaded or loading takes them and reloads to start them. Otherwise they would
    stop until a restart. The YAML entities `leaving` registered are removed
    first, so the next entry creates them afresh, enabled, with the entity ids
    and settings the registry kept for them.
    """
    yaml = hass.data.get(YAML_WATCHES)
    if yaml is None or not yaml.raw or hass.is_stopping:
        return
    if yaml.holder is None:
        first = _first_in_line(hass)
        if first is None or first.entry_id != leaving.entry_id:
            return
    elif yaml.holder != leaving.entry_id:
        return
    registry = er.async_get(hass)
    for registered in er.async_entries_for_config_entry(registry, leaving.entry_id):
        if registered.unique_id.startswith(f"{YAML_ID_SCOPE}:"):
            registry.async_remove(registered.entity_id)
    yaml.holder = None
    for candidate in hass.config_entries.async_entries(DOMAIN, include_disabled=False):
        if candidate.entry_id == leaving.entry_id:
            continue
        if candidate.state in (ConfigEntryState.LOADED, ConfigEntryState.SETUP_IN_PROGRESS):
            yaml.holder = candidate.entry_id
            hass.config_entries.async_schedule_reload(candidate.entry_id)
            return


async def async_migrate_entry(hass: HomeAssistant, entry: MentatConfigEntry) -> bool:
    """Accept every version 1 entry as it is. There has only been 1.1.

    Home Assistant refuses an entry from a newer major version before this runs.
    """
    return entry.version == 1


def _ledger_store(hass: HomeAssistant, entry: ConfigEntry) -> Store[dict[str, Any]]:
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.ledger")


async def _async_load_ledger(
    hass: HomeAssistant, entry: MentatConfigEntry, provider_key: str, model: str
) -> Ledger:
    """The entry's ledger with today's stored totals, if the store has today's."""
    store = _ledger_store(hass, entry)
    options = entry.options
    ledger = Ledger(
        hass,
        entry.entry_id,
        day=dt_util.now().date(),
        budget=options.get(CONF_TOKEN_BUDGET, 0),
        token_price=options.get(CONF_TOKEN_PRICE, price_for(provider_key, model)),
        store=store,
    )
    ledger.adopt(await store.async_load())
    return ledger


@callback
def _roll_over_at_midnight(hass: HomeAssistant, entry: MentatConfigEntry, ledger: Ledger) -> None:
    """Start a new day at local midnight, not at the first request after it.

    Waiting for a request would keep yesterday's spend, and yesterday's repair
    issue, through a quiet night.
    """

    @callback
    def _midnight(now: datetime) -> None:
        ledger.roll_over(now.date())

    entry.async_on_unload(async_track_time_change(hass, _midnight, hour=0, minute=0, second=0))


async def _async_probe(runtime: MentatRuntimeData) -> None:
    """Ask one noul about a two letter state, and fail setup if it is not answered.

    The probe is billed like any other request, so it counts against the day
    and a spent budget skips it. It records no body size: the request is
    mostly fixed overhead, and its ratio of bytes to tokens would skew the
    estimate for real ones.
    """
    if runtime.ledger.over_budget:
        return
    try:
        reply = await runtime.client.ask(PROBE_STATE, {PROBE_KEY: Noul(PROBE_INSTRUCTIONS)})
    except JevError as err:
        placeholders = {"provider": runtime.provider.title, "reason": str(err)}
        if isinstance(err, JevAuthError):
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="key_refused",
                translation_placeholders=placeholders,
            ) from err
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="setup_failed",
            translation_placeholders=placeholders,
        ) from err
    runtime.ledger.record(reply.usage.input_tokens)
    runtime.model_version = reply.model or None


async def _async_start_watch(coordinator: MentatCoordinator) -> None:
    """Start the watch's triggers, and ask once if anything reads it.

    Triggers come first: a window watch's first evaluation reads the log that
    starting them fills from the recorder. The first evaluation is not
    async_config_entry_first_refresh, which aborts setup when it fails. Its two
    ways to fail are a spent budget and an unreachable server, and the entities
    that explain both exist only once setup finishes, so the answers start
    unavailable instead. A refused key still starts reauth from inside it.
    """
    await coordinator.async_setup_triggers()
    if not coordinator.nobody_reads():
        await coordinator.async_request_evaluation()


async def _async_start_watches(hass: HomeAssistant, entry: MentatConfigEntry) -> None:
    """Start every watch at once, so setup waits for the slowest first answer, not their sum.

    Every start runs to its end before a failure is raised. The triggers a
    finished start left behind are then stopped by runtime.async_stop, which
    setup registered to run when it fails, and no start can add one after it.
    """
    runtime = entry.runtime_data
    watches = [*_yaml_watches(hass, entry), *watches_from_subentries(hass, entry)]
    for watch in watches:
        runtime.coordinators[watch.key] = MentatCoordinator(hass, entry, runtime, watch)
    outcomes = await asyncio.gather(
        *(_async_start_watch(coordinator) for coordinator in runtime.coordinators.values()),
        return_exceptions=True,
    )
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            raise outcome


async def async_setup_entry(hass: HomeAssistant, entry: MentatConfigEntry) -> bool:
    """Set the entry up. On failure, another entry takes the YAML watches if this one had them."""
    try:
        await _async_set_up(hass, entry)
    except BaseException:
        _pass_on_yaml(hass, entry)
        raise
    return True


async def _async_set_up(hass: HomeAssistant, entry: MentatConfigEntry) -> None:
    """Build the client and the ledger, prove the endpoint answers, then start the watches."""
    data = entry.data
    provider = provider_for(data)
    model: str = data.get(CONF_MODEL) or provider.model
    client = build_client(
        provider,
        api_key=data.get(CONF_API_KEY, ""),
        base_url=data.get(CONF_URL),
        model=model,
        account_id=data.get(CONF_ACCOUNT_ID),
        image_format=provider.image_format,
        extra=data.get(CONF_EXTRA),
        session=async_get_clientsession(hass),
    )
    ledger = await _async_load_ledger(hass, entry, provider.key, model)
    runtime = MentatRuntimeData(provider=provider, client=client, model=model, ledger=ledger)
    entry.runtime_data = runtime
    # Home Assistant runs this on unload, after the platforms, and also when
    # setup fails. A watch started before a later failure would otherwise keep
    # listening, and keep asking, for an entry that never loaded.
    entry.async_on_unload(runtime.async_stop)
    _roll_over_at_midnight(hass, entry, ledger)
    await _async_probe(runtime)
    await _async_start_watches(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_entry_changed))


async def _async_entry_changed(hass: HomeAssistant, entry: MentatConfigEntry) -> None:
    """Options or questions changed. A reload rebuilds every watch from them."""
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: MentatConfigEntry) -> bool:
    """Unload the platforms. The watches, the ledger and the client stop after this returns.

    runtime.async_stop does that from the unload callbacks. A disabled entry
    will not come back, so its YAML watches move on now.
    """
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    if entry.disabled_by is not None:
        _pass_on_yaml(hass, entry)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: MentatConfigEntry) -> None:
    """Delete what would outlive the entry: its stored ledger and its over-budget issue."""
    ir.async_delete_issue(hass, DOMAIN, over_budget_issue_id(entry.entry_id))
    await _ledger_store(hass, entry).async_remove()
    _pass_on_yaml(hass, entry)
