"""Config, reauth, reconfigure and options flows.

Setup sends one real probe question. It costs a fraction of a cent on a hosted
provider and nothing on a local one, and it is the only way to tell a working key,
address and dialect from a typed one before any entity appears.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
)
from homeassistant.const import CONF_API_KEY, CONF_MODEL, CONF_URL
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from jevclient import (
    JevAuthError,
    JevConnectionError,
    JevError,
    JevValidationError,
    Noul,
)
from yarl import URL

from .client import WrongDialectError, build_client
from .const import (
    CONF_ACCOUNT_ID,
    CONF_ADVANCED,
    CONF_DIALECT,
    CONF_EXTRA,
    CONF_IMAGE_FORMAT,
    CONF_MAX_IMAGES,
    CONF_PROVIDER,
    CONF_TOKEN_BUDGET,
    CONF_TOKEN_PRICE,
    CONF_TRUNCATE,
    DEFAULT_MAX_IMAGES,
    DOMAIN,
    MAX_IMAGES,
    PROBE_INSTRUCTIONS,
    PROBE_KEY,
    PROBE_STATE,
    SUBENTRY_QUESTION,
)
from .editor import MentatQuestionSubentryFlow
from .providers import (
    PROVIDER_GENERIC,
    PROVIDER_LOCAL_CLEF,
    PROVIDER_TYPESAFE,
    PROVIDERS,
    Dialect,
    ImageFormat,
    Provider,
    default_image_format,
    entry_unique_id,
    model_offered,
    price_for,
    provider_for,
    resolve_url,
)

_LOGGER = logging.getLogger(__name__)

type _SubentryFlows = dict[str, type[ConfigSubentryFlow]]

# Questions are added one at a time from the entry's page.
SUBENTRY_FLOWS: Final[_SubentryFlows] = {SUBENTRY_QUESTION: MentatQuestionSubentryFlow}

# A Cloudflare account id is 32 hex digits, as the dashboard shows it.
_ACCOUNT_ID: Final = re.compile(r"[0-9a-fA-F]{32}")

_PASSWORD: Final = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
)

# Collapsed, so the common setup is a provider and a key. No field has a default:
# a default would be one provider's value, and it would win after switching to
# another. Fields a provider has no use for are ignored for it.
ADVANCED_SCHEMA: Final = vol.Schema(
    {
        vol.Optional(CONF_URL): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
        ),
        vol.Optional(CONF_MODEL): str,
        vol.Optional(CONF_DIALECT): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[dialect.value for dialect in Dialect],
                translation_key=CONF_DIALECT,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Optional(CONF_IMAGE_FORMAT): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[image_format.value for image_format in ImageFormat],
                translation_key=CONF_IMAGE_FORMAT,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Optional(CONF_TRUNCATE): bool,
    }
)

# The key is optional because a server of your own may need none. An empty key for
# a provider that needs one is refused below rather than sent.
STEP_USER_SCHEMA: Final = vol.Schema(
    {
        vol.Required(CONF_PROVIDER, default=PROVIDER_TYPESAFE): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=list(PROVIDERS),
                translation_key=CONF_PROVIDER,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Optional(CONF_API_KEY, default=""): _PASSWORD,
        vol.Required(CONF_ADVANCED): section(ADVANCED_SCHEMA, {"collapsed": True}),
    }
)

STEP_ACCOUNT_SCHEMA: Final = vol.Schema({vol.Required(CONF_ACCOUNT_ID): str})

# Reauth is a rejected credential, not a moved address, so it asks for the key
# alone. A moved address is a reconfigure.
STEP_REAUTH_SCHEMA: Final = vol.Schema({vol.Required(CONF_API_KEY): _PASSWORD})


def _has_space(text: str) -> bool:
    return any(character.isspace() for character in text)


def _not_a_web_address(text: str) -> bool:
    url = URL(text)
    return url.scheme not in ("http", "https") or not url.host


def _carries_extras(text: str) -> bool:
    # A query or fragment cannot survive the path the client appends.
    # Credentials are refused because diagnostics redacts by key name and would
    # miss them inside an address.
    url = URL(text)
    return bool(url.query_string or url.fragment or url.user or url.password)


@dataclass(frozen=True, slots=True)
class _TypedText:
    """A typed text field: how to tell a paste error, and how to store it."""

    refusals: tuple[Callable[[str], bool], ...]
    stored: Callable[[str], str] = str

    def clean(self, raw: str | None) -> str | None:
        """The value to store, None when nothing was typed, or ValueError."""
        text = (raw or "").strip()
        if not text:
            return None
        if any(refuse(text) for refuse in self.refusals):
            raise ValueError(text)
        return self.stored(text)


# A path is allowed in an address, since a reverse proxy can mount the API under one.
_ADDRESS: Final = _TypedText(
    (_has_space, _not_a_web_address, _carries_extras),
    stored=lambda text: str(URL(text)).rstrip("/"),
)
_MODEL_ID: Final = _TypedText((_has_space,))
_normalised_url: Final = _ADDRESS.clean
_normalised_model: Final = _MODEL_ID.clean


def _parse_form(user_input: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """The entry data a user or reconfigure form describes, and what is wrong with it.

    Every field is checked before the probe, so two typos are two messages on one
    submit rather than one per attempt.
    """
    name = user_input.get(CONF_PROVIDER, PROVIDER_TYPESAFE)
    preset = PROVIDERS[name]
    advanced = user_input.get(CONF_ADVANCED) or {}
    errors: dict[str, str] = {}

    api_key = (user_input.get(CONF_API_KEY) or "").strip()
    if preset.key_required and not api_key:
        errors[CONF_API_KEY] = "key_missing"

    url = preset.base_url
    # Cloudflare's address is a fixed template; only the account id varies.
    if not preset.needs_account_id:
        try:
            url = _normalised_url(advanced.get(CONF_URL)) or url
        except ValueError:
            errors[CONF_URL] = "address_unusable"
    if not url and CONF_URL not in errors:
        errors[CONF_URL] = "address_missing"

    model = preset.model
    try:
        model = _normalised_model(advanced.get(CONF_MODEL)) or model
    except ValueError:
        errors[CONF_MODEL] = "model_refused"
    if CONF_MODEL not in errors and not model_offered(preset, model):
        # Named without spending a request on it.
        errors[CONF_MODEL] = "model_refused"

    dialect, image_format = preset.dialect, preset.image_format
    if name == PROVIDER_GENERIC:
        dialect = Dialect(advanced.get(CONF_DIALECT) or preset.dialect)
        image_format = ImageFormat(advanced.get(CONF_IMAGE_FORMAT) or default_image_format(dialect))
        # TypeSafe sends assets and nothing else; Clef reads images inline only.
        if dialect is Dialect.TYPESAFE:
            image_format = ImageFormat.ASSETS
        elif image_format is ImageFormat.ASSETS:
            image_format = ImageFormat.DATA_URL

    data: dict[str, Any] = {
        CONF_PROVIDER: name,
        CONF_API_KEY: api_key,
        CONF_URL: url,
        CONF_MODEL: model,
        CONF_DIALECT: dialect.value,
        CONF_IMAGE_FORMAT: image_format.value,
    }
    if name == PROVIDER_LOCAL_CLEF:
        # clef_mlx.py reads a truncate flag. The ha-clef replays send false so the
        # model reads the whole state, and that is the default here too.
        data[CONF_EXTRA] = {"truncate": bool(advanced.get(CONF_TRUNCATE, False))}
    return data, errors


def _unique_id(data: Mapping[str, Any]) -> str:
    provider = provider_for(data)
    return entry_unique_id(
        resolve_url(provider, data.get(CONF_URL), data.get(CONF_ACCOUNT_ID), None),
        data.get(CONF_API_KEY, ""),
    )


def _generic_suggestions(data: Mapping[str, Any], preset: Provider) -> dict[str, Any]:
    return {
        CONF_DIALECT: data.get(CONF_DIALECT, preset.dialect.value),
        CONF_IMAGE_FORMAT: data.get(CONF_IMAGE_FORMAT, preset.image_format.value),
    }


def _clef_suggestions(data: Mapping[str, Any], preset: Provider) -> dict[str, Any]:
    return {CONF_TRUNCATE: bool((data.get(CONF_EXTRA) or {}).get("truncate"))}


# Advanced fields a provider always shows, whatever their value.
_ALWAYS_SUGGESTED: Final[dict[str, Callable[[Mapping[str, Any], Provider], dict[str, Any]]]] = {
    PROVIDER_GENERIC: _generic_suggestions,
    PROVIDER_LOCAL_CLEF: _clef_suggestions,
}


def _suggest(data: Mapping[str, Any]) -> dict[str, Any]:
    """Form values for an entry, shaped the way add_suggested_values_to_schema reads.

    The address and the model go in the section only when they differ from the
    provider's own. Shown always, they would outlive a switch of provider and
    send the new provider's key to the old provider's address.
    """
    name = data.get(CONF_PROVIDER, PROVIDER_GENERIC)
    preset = PROVIDERS.get(name) or PROVIDERS[PROVIDER_GENERIC]
    own = {CONF_URL: preset.base_url, CONF_MODEL: preset.model}
    advanced = {
        key: data[key] for key, value in own.items() if data.get(key) not in (None, "", value)
    }
    if (extra := _ALWAYS_SUGGESTED.get(name)) is not None:
        advanced |= extra(data, preset)
    return {CONF_PROVIDER: name, CONF_ADVANCED: advanced}


# The form error for a failed probe, by exception type, first match first.
# WrongDialectError is a response error, so it comes before the status codes.
_PROBE_ERRORS: Final[tuple[tuple[type[JevError], str], ...]] = (
    (JevAuthError, "invalid_auth"),
    (WrongDialectError, "wrong_dialect"),
    # The probe's question is fixed, so the model id is the only part of the
    # request a typo can reach.
    (JevValidationError, "model_refused"),
    (JevConnectionError, "cannot_connect"),
)
# Then by HTTP status. OpenRouter answers 402 to a key on an account with no
# credit, and the client maps Cloudflare's spent free allocation to it. A 404
# means the host answered with nothing on that path: one dialect pointed at the
# other's address.
_STATUS_ERRORS: Final = {402: "out_of_credit", 404: "wrong_dialect"}


def _error_key(err: JevError) -> str:
    """The form error for a failed probe."""
    found = (key for kind, key in _PROBE_ERRORS if isinstance(err, kind))
    return next(found, None) or _STATUS_ERRORS.get(err.status or 0, "server_error")


class MentatConfigFlow(ConfigFlow, domain=DOMAIN):
    """Take a provider, a key and an address, and prove the three work together."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        # What the server said on the last failed probe, for the server_error text.
        self._reply = ""
        # The validated form of the step before the account id step.
        self._pending: dict[str, Any] = {}
        self._suggested_account_id = ""

    def _placeholders(self) -> dict[str, str]:
        return {"reply": self._reply}

    async def _async_probe(self, data: Mapping[str, Any]) -> str | None:
        """An error key, or None when the endpoint answers the probe."""
        provider = provider_for(data)
        client = build_client(
            provider,
            api_key=data.get(CONF_API_KEY, ""),
            base_url=data.get(CONF_URL),
            model=data.get(CONF_MODEL),
            account_id=data.get(CONF_ACCOUNT_ID),
            image_format=provider.image_format,
            extra=data.get(CONF_EXTRA),
            session=async_get_clientsession(self.hass),
        )
        try:
            await client.ask(PROBE_STATE, {PROBE_KEY: Noul(PROBE_INSTRUCTIONS)})
        except JevError as err:
            _LOGGER.warning("Setup check against %s failed: %s", provider.title, err)
            self._reply = str(err)
            return _error_key(err)
        finally:
            await client.async_close()
        return None

    async def _async_update_entry(
        self, entry: ConfigEntry, data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Store proven data on an existing entry, moving its unique id with it.

        The id hashes the address and the key, so a new pair needs a new id. Only
        another entry already holding that id is a collision; this entry holding
        it is a key typed again.
        """
        new_id = _unique_id(data)
        for other in self._async_current_entries(include_ignore=True):
            if other.entry_id != entry.entry_id and other.unique_id == new_id:
                return self.async_abort(reason="already_configured")
        await self.async_set_unique_id(new_id)
        return self.async_update_reload_and_abort(entry, unique_id=new_id, data=data)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = _parse_form(user_input)
            if not errors:
                if PROVIDERS[data[CONF_PROVIDER]].needs_account_id:
                    self._pending = data
                    return await self.async_step_account_id()
                await self.async_set_unique_id(_unique_id(data))
                self._abort_if_unique_id_configured()
                if (error := await self._async_probe(data)) is None:
                    return self._async_create(data)
                errors["base"] = error
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA,
                # What was typed, so it can be corrected rather than retyped. The
                # key is not handed back: it is a secret.
                {
                    CONF_PROVIDER: user_input.get(CONF_PROVIDER, PROVIDER_TYPESAFE),
                    CONF_ADVANCED: user_input.get(CONF_ADVANCED, {}),
                }
                if user_input
                else {},
            ),
            errors=errors,
            description_placeholders=self._placeholders(),
        )

    async def async_step_account_id(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Cloudflare only: the account whose Workers AI answers."""
        errors: dict[str, str] = {}
        suggested = self._suggested_account_id
        if user_input is not None:
            account_id = (user_input.get(CONF_ACCOUNT_ID) or "").strip()
            suggested = account_id
            if not account_id:
                errors[CONF_ACCOUNT_ID] = "account_id_required"
            elif not _ACCOUNT_ID.fullmatch(account_id):
                errors[CONF_ACCOUNT_ID] = "invalid_account_id"
            else:
                data = {**self._pending, CONF_ACCOUNT_ID: account_id}
                if self.source == SOURCE_RECONFIGURE:
                    if (error := await self._async_probe(data)) is None:
                        return await self._async_update_entry(self._get_reconfigure_entry(), data)
                else:
                    await self.async_set_unique_id(_unique_id(data))
                    self._abort_if_unique_id_configured()
                    if (error := await self._async_probe(data)) is None:
                        return self._async_create(data)
                errors["base"] = error
        return self.async_show_form(
            step_id="account_id",
            data_schema=self.add_suggested_values_to_schema(
                STEP_ACCOUNT_SCHEMA, {CONF_ACCOUNT_ID: suggested} if suggested else {}
            ),
            errors=errors,
            description_placeholders=self._placeholders(),
        )

    @callback
    def _async_create(self, data: dict[str, Any]) -> ConfigFlowResult:
        return self.async_create_entry(title=PROVIDERS[data[CONF_PROVIDER]].title, data=data)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            api_key = (user_input.get(CONF_API_KEY) or "").strip()
            if not api_key and provider_for(entry.data).key_required:
                errors[CONF_API_KEY] = "key_missing"
            else:
                data = {**entry.data, CONF_API_KEY: api_key}
                if (error := await self._async_probe(data)) is None:
                    return await self._async_update_entry(entry, data)
                errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={**self._placeholders(), "title": entry.title},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the provider, key or address and keep the entities."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = _parse_form(user_input)
            # The key is never shown back, so an empty field while the provider and
            # the address stay put means keep it. A new address gets no key it was
            # not given: the key would travel to a host it was not typed for.
            same_place = data[CONF_PROVIDER] == entry.data.get(CONF_PROVIDER) and data[
                CONF_URL
            ] == entry.data.get(CONF_URL)
            if not data[CONF_API_KEY] and same_place:
                data[CONF_API_KEY] = entry.data.get(CONF_API_KEY, "")
                errors.pop(CONF_API_KEY, None)
            if not errors:
                if PROVIDERS[data[CONF_PROVIDER]].needs_account_id:
                    self._pending = data
                    self._suggested_account_id = entry.data.get(CONF_ACCOUNT_ID, "")
                    return await self.async_step_account_id()
                if (error := await self._async_probe(data)) is None:
                    return await self._async_update_entry(entry, data)
                errors["base"] = error
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(STEP_USER_SCHEMA, _suggest(entry.data)),
            errors=errors,
            description_placeholders=self._placeholders(),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> MentatOptionsFlow:
        return MentatOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, entry: ConfigEntry) -> _SubentryFlows:
        return dict(SUBENTRY_FLOWS)


class MentatOptionsFlow(OptionsFlow):
    """Spending limits and how many images one request may carry.

    The budget is a tripwire, not a quota: set it past anything a working setup
    uses, so only a runaway reaches it.
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        provider = provider_for(self.config_entry.data)
        model = self.config_entry.data.get(CONF_MODEL) or provider.model
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_TOKEN_BUDGET,
                    default=options.get(CONF_TOKEN_BUDGET, 0),
                ): vol.All(vol.Coerce(int), vol.Range(min=0)),
                vol.Optional(
                    CONF_TOKEN_PRICE,
                    default=options.get(CONF_TOKEN_PRICE, price_for(provider.key, model)),
                ): vol.All(vol.Coerce(float), vol.Range(min=0)),
                vol.Optional(
                    CONF_MAX_IMAGES,
                    default=options.get(CONF_MAX_IMAGES, DEFAULT_MAX_IMAGES),
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=MAX_IMAGES)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
