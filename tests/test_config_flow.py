"""The config, reauth, reconfigure and options flows."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_API_KEY, CONF_MODEL, CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult, FlowResultType, InvalidData
from jevclient import (
    JevAuthError,
    JevConnectionError,
    JevError,
    JevOverloadedError,
    JevRateLimitError,
    JevResponseError,
    JevValidationError,
    Noul,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mentat.client import WrongDialectError
from custom_components.mentat.config_flow import _unique_id
from custom_components.mentat.const import (
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
    DOMAIN,
)
from custom_components.mentat.providers import PROVIDERS, entry_unique_id

from .conftest import ACCOUNT_ID, API_KEY, ENTRY_DATA, LOCAL_URL

TYPESAFE_URL = "https://api.typesafe.ai"
CLOUDFLARE_TEMPLATE = PROVIDERS["cloudflare"].base_url
GENERIC_URL = ENTRY_DATA["generic"][CONF_URL]


def typed(provider: str = "typesafe", key: str = API_KEY, **advanced: Any) -> dict[str, Any]:
    """A user or reconfigure form as submitted: the advanced section is its own dict."""
    return {CONF_PROVIDER: provider, CONF_API_KEY: key, CONF_ADVANCED: advanced}


async def submit(hass: HomeAssistant, form: dict[str, Any]) -> FlowResult:
    """Open the user step and submit one form to it."""
    opened = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert opened["type"] is FlowResultType.FORM
    assert opened["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(opened["flow_id"], form)


async def answer(hass: HomeAssistant, result: FlowResult, form: dict[str, Any]) -> FlowResult:
    return await hass.config_entries.flow.async_configure(result["flow_id"], form)


def offered(result: FlowResult) -> tuple[str, dict[str, Any]]:
    """The provider and the advanced values a form shows back."""
    schema = result["data_schema"].schema
    provider = next(key for key in schema if key.schema == CONF_PROVIDER)
    section = schema[CONF_ADVANCED].schema.schema
    return (
        provider.description["suggested_value"],
        {
            key.schema: key.description["suggested_value"]
            for key in section
            if key.description and "suggested_value" in key.description
        },
    )


# --- the user step, one provider at a time ---


async def test_flow_typesafe_probes_and_creates_the_entry(
    hass: HomeAssistant, fake_client: MagicMock
) -> None:
    result = await submit(hass, typed())

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "TypeSafe Jev"
    assert result["data"] == ENTRY_DATA["typesafe"]
    assert result["result"].unique_id == entry_unique_id(TYPESAFE_URL, API_KEY)
    # The flow's probe, then setup's.
    assert fake_client.ask.await_count == 2
    state, questions = fake_client.ask.await_args_list[0].args
    assert state == "ok"
    assert questions == {"probe": Noul("Is this text in English?")}
    built = fake_client.built_by_flow.call_args.kwargs
    assert (built["api_key"], built["base_url"], built["account_id"]) == (
        API_KEY,
        TYPESAFE_URL,
        None,
    )
    fake_client.async_close.assert_awaited()


async def test_flow_openrouter_trims_the_key(hass: HomeAssistant, fake_client: MagicMock) -> None:
    result = await submit(hass, typed("openrouter", f"\t{API_KEY} \n"))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "OpenRouter"
    assert result["data"] == ENTRY_DATA["openrouter"]


async def test_flow_local_clef_takes_no_key_and_stores_truncate(
    hass: HomeAssistant, fake_client: MagicMock
) -> None:
    result = await submit(hass, typed("local_clef", "", url=f"{LOCAL_URL}/", truncate=True))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {**ENTRY_DATA["local_clef"], CONF_EXTRA: {"truncate": True}}


async def test_flow_local_clef_without_an_address_uses_its_own(
    hass: HomeAssistant, fake_client: MagicMock
) -> None:
    result = await submit(hass, typed("local_clef", ""))
    assert result["data"][CONF_URL] == "http://127.0.0.1:8000"
    assert result["data"][CONF_EXTRA] == {"truncate": False}


async def test_flow_generic_typesafe_server_stores_assets_whatever_was_picked(
    hass: HomeAssistant, fake_client: MagicMock
) -> None:
    result = await submit(hass, typed("generic", "", url=GENERIC_URL, image_format="data_url"))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "System One server"
    assert result["data"] == ENTRY_DATA["generic"]


@pytest.mark.parametrize(
    ("picked", "stored"),
    [(None, "data_url"), ("assets", "data_url"), ("base64", "base64")],
)
async def test_flow_generic_clef_server_sends_pictures_inline(
    hass: HomeAssistant, fake_client: MagicMock, picked: str | None, stored: str
) -> None:
    advanced: dict[str, Any] = {CONF_URL: "http://198.51.100.30", CONF_DIALECT: "clef"}
    if picked:
        advanced[CONF_IMAGE_FORMAT] = picked
    result = await submit(hass, typed("generic", "", **advanced))
    assert result["data"][CONF_DIALECT] == "clef"
    assert result["data"][CONF_IMAGE_FORMAT] == stored


async def test_flow_cloudflare_asks_the_account_before_any_request(
    hass: HomeAssistant, fake_client: MagicMock
) -> None:
    result = await submit(hass, typed("cloudflare", model="clef"))
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "account_id"
    assert fake_client.ask.await_count == 0

    result = await answer(hass, result, {CONF_ACCOUNT_ID: f"  {ACCOUNT_ID}\t"})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Cloudflare Workers AI"
    assert result["data"] == {**ENTRY_DATA["cloudflare"], CONF_MODEL: "clef"}
    # The model stays a placeholder in the unique id, so clef and clef-flash on
    # one account are one entry.
    assert result["result"].unique_id == entry_unique_id(
        CLOUDFLARE_TEMPLATE.replace("{account_id}", ACCOUNT_ID), API_KEY
    )
    assert fake_client.built_by_flow.call_args.kwargs["account_id"] == ACCOUNT_ID


@pytest.mark.parametrize(
    ("account_id", "error"),
    [("\t ", "account_id_required"), ("abc123", "invalid_account_id")],
)
async def test_flow_cloudflare_account_id_is_checked_without_a_request(
    hass: HomeAssistant, fake_client: MagicMock, account_id: str, error: str
) -> None:
    result = await submit(hass, typed("cloudflare"))
    result = await answer(hass, result, {CONF_ACCOUNT_ID: account_id})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_ACCOUNT_ID: error}
    assert fake_client.ask.await_count == 0


async def test_flow_cloudflare_probe_failure_stays_on_the_account_step(
    hass: HomeAssistant, fake_client: MagicMock
) -> None:
    fake_client.ask.side_effect = JevAuthError("token lacks Workers AI", status=403)
    result = await submit(hass, typed("cloudflare"))
    result = await answer(hass, result, {CONF_ACCOUNT_ID: ACCOUNT_ID})
    assert result["step_id"] == "account_id"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_flow_cloudflare_account_already_set_up_aborts(
    hass: HomeAssistant, fake_client: MagicMock
) -> None:
    MockConfigEntry(
        domain=DOMAIN,
        data=ENTRY_DATA["cloudflare"],
        unique_id=_unique_id(ENTRY_DATA["cloudflare"]),
    ).add_to_hass(hass)
    result = await submit(hass, typed("cloudflare"))
    result = await answer(hass, result, {CONF_ACCOUNT_ID: ACCOUNT_ID})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# --- the user step, mistakes caught before any request ---


@pytest.mark.parametrize(
    ("form", "errors"),
    [
        (typed(key=" \t"), {CONF_API_KEY: "key_missing"}),
        (typed("cloudflare", ""), {CONF_API_KEY: "key_missing"}),
        (typed(url="gopher://198.51.100.1"), {CONF_URL: "address_unusable"}),
        (typed(url="http://198.51.100.1/v 1"), {CONF_URL: "address_unusable"}),
        (typed(url="https://198.51.100.1/#top"), {CONF_URL: "address_unusable"}),
        (typed(url="https://me:secret@198.51.100.1"), {CONF_URL: "address_unusable"}),
        (typed("generic", ""), {CONF_URL: "address_missing"}),
        (typed(model="jev 1.14"), {CONF_MODEL: "model_refused"}),
        (typed("cloudflare", model="mistral"), {CONF_MODEL: "model_refused"}),
        (
            typed("generic", "", model="two words"),
            {CONF_URL: "address_missing", CONF_MODEL: "model_refused"},
        ),
    ],
)
async def test_flow_form_mistakes_are_named_without_a_request(
    hass: HomeAssistant, fake_client: MagicMock, form: dict[str, Any], errors: dict[str, str]
) -> None:
    result = await submit(hass, form)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == errors
    assert fake_client.ask.await_count == 0
    # What was typed comes back, to be corrected rather than retyped.
    assert offered(result) == (form[CONF_PROVIDER], form[CONF_ADVANCED])


# --- the user step, a probe that fails ---


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (JevAuthError("unknown key", status=401), "invalid_auth"),
        (WrongDialectError("no answers envelope"), "wrong_dialect"),
        (JevValidationError("model not found", status=422), "model_refused"),
        (JevConnectionError("timed out"), "cannot_connect"),
        (JevResponseError("HTTP 402: top up", status=402), "out_of_credit"),
        (JevResponseError("HTTP 404: nothing here", status=404), "wrong_dialect"),
        (JevResponseError("HTTP 503: down", status=503), "server_error"),
        (JevRateLimitError("too many", retry_after=9.0), "server_error"),
        (JevOverloadedError("overloaded", status=529), "server_error"),
    ],
)
async def test_flow_probe_failure_is_shown_and_a_retry_succeeds(
    hass: HomeAssistant, fake_client: MagicMock, error: JevError, key: str
) -> None:
    fake_client.ask.side_effect = error
    result = await submit(hass, typed())
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": key}
    assert result["description_placeholders"] == {"reply": str(error)}
    fake_client.async_close.assert_awaited()

    fake_client.ask.side_effect = None
    result = await answer(hass, result, typed())
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_flow_same_key_and_address_twice_aborts(
    hass: HomeAssistant, fake_client: MagicMock
) -> None:
    assert (await submit(hass, typed()))["type"] is FlowResultType.CREATE_ENTRY
    result = await submit(hass, typed())
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_flow_same_key_at_another_address_is_a_second_entry(
    hass: HomeAssistant, fake_client: MagicMock
) -> None:
    assert (await submit(hass, typed()))["type"] is FlowResultType.CREATE_ENTRY
    result = await submit(hass, typed(url="https://198.51.100.40"))
    assert result["type"] is FlowResultType.CREATE_ENTRY


# --- reauth ---


async def open_reauth(hass: HomeAssistant, entry: MockConfigEntry) -> FlowResult:
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"]["title"] == entry.title
    return result


async def test_reauth_new_key_is_stored_and_moves_the_unique_id(
    hass: HomeAssistant, fake_client: MagicMock, loaded: MockConfigEntry
) -> None:
    result = await open_reauth(hass, loaded)
    result = await answer(hass, result, {CONF_API_KEY: "\tfresh-key "})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert loaded.data[CONF_API_KEY] == "fresh-key"
    assert loaded.data[CONF_URL] == TYPESAFE_URL
    assert loaded.unique_id == entry_unique_id(TYPESAFE_URL, "fresh-key")


async def test_reauth_blank_key_is_refused_where_one_is_needed(
    hass: HomeAssistant, fake_client: MagicMock, loaded: MockConfigEntry
) -> None:
    result = await open_reauth(hass, loaded)
    before = fake_client.ask.await_count
    result = await answer(hass, result, {CONF_API_KEY: "   "})
    assert result["errors"] == {CONF_API_KEY: "key_missing"}
    assert fake_client.ask.await_count == before


async def test_reauth_probe_failure_keeps_the_old_key(
    hass: HomeAssistant, fake_client: MagicMock, loaded: MockConfigEntry
) -> None:
    result = await open_reauth(hass, loaded)
    fake_client.ask.side_effect = JevAuthError("still unknown", status=401)
    result = await answer(hass, result, {CONF_API_KEY: "typo-key"})
    assert result["errors"] == {"base": "invalid_auth"}
    assert loaded.data[CONF_API_KEY] == API_KEY


@pytest.mark.parametrize("provider_key", ["local_clef"], indirect=True)
async def test_reauth_keyless_server_accepts_a_blank_key(
    hass: HomeAssistant, fake_client: MagicMock, loaded: MockConfigEntry
) -> None:
    result = await open_reauth(hass, loaded)
    result = await answer(hass, result, {CONF_API_KEY: ""})
    assert result["reason"] == "reauth_successful"


async def test_reauth_key_held_by_another_entry_aborts(
    hass: HomeAssistant, fake_client: MagicMock, loaded: MockConfigEntry
) -> None:
    MockConfigEntry(
        domain=DOMAIN,
        data={**ENTRY_DATA["typesafe"], CONF_API_KEY: "neighbour-key"},
        unique_id=entry_unique_id(TYPESAFE_URL, "neighbour-key"),
    ).add_to_hass(hass)
    result = await open_reauth(hass, loaded)
    result = await answer(hass, result, {CONF_API_KEY: "neighbour-key"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert loaded.data[CONF_API_KEY] == API_KEY


# --- reconfigure ---


async def open_reconfigure(hass: HomeAssistant, entry: MockConfigEntry) -> FlowResult:
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    return result


@pytest.mark.parametrize(
    ("provider_key", "shown"),
    [
        ("typesafe", {}),
        ("cloudflare", {}),
        ("local_clef", {CONF_URL: LOCAL_URL, CONF_TRUNCATE: False}),
        (
            "generic",
            {CONF_URL: GENERIC_URL, CONF_DIALECT: "typesafe", CONF_IMAGE_FORMAT: "assets"},
        ),
    ],
    indirect=["provider_key"],
)
async def test_reconfigure_shows_only_what_differs_from_the_preset(
    hass: HomeAssistant,
    fake_client: MagicMock,
    loaded: MockConfigEntry,
    provider_key: str,
    shown: dict[str, Any],
) -> None:
    result = await open_reconfigure(hass, loaded)
    assert offered(result) == (provider_key, shown)


async def test_reconfigure_shows_a_model_of_your_own(
    hass: HomeAssistant, fake_client: MagicMock
) -> None:
    pinned = MockConfigEntry(
        domain=DOMAIN, data={**ENTRY_DATA["typesafe"], CONF_MODEL: "jev-1.12.4"}
    )
    pinned.add_to_hass(hass)
    result = await open_reconfigure(hass, pinned)
    assert offered(result) == ("typesafe", {CONF_MODEL: "jev-1.12.4"})


async def test_reconfigure_entry_without_a_provider_reads_as_generic(
    hass: HomeAssistant, fake_client: MagicMock
) -> None:
    bare = MockConfigEntry(domain=DOMAIN, data={CONF_URL: "http://198.51.100.50"})
    bare.add_to_hass(hass)
    result = await open_reconfigure(hass, bare)
    assert offered(result) == (
        "generic",
        {CONF_URL: "http://198.51.100.50", CONF_DIALECT: "typesafe", CONF_IMAGE_FORMAT: "assets"},
    )


async def test_reconfigure_blank_key_at_the_same_address_keeps_the_key(
    hass: HomeAssistant, fake_client: MagicMock, loaded: MockConfigEntry
) -> None:
    result = await open_reconfigure(hass, loaded)
    result = await answer(hass, result, typed(key="", model="jev-1.12.4"))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert loaded.data[CONF_API_KEY] == API_KEY
    assert loaded.data[CONF_MODEL] == "jev-1.12.4"
    assert fake_client.built_by_flow.call_args.kwargs["api_key"] == API_KEY


async def test_reconfigure_blank_key_is_not_carried_to_a_new_address(
    hass: HomeAssistant, fake_client: MagicMock, loaded: MockConfigEntry
) -> None:
    result = await open_reconfigure(hass, loaded)
    result = await answer(hass, result, typed(key="", url="https://198.51.100.60"))
    assert result["errors"] == {CONF_API_KEY: "key_missing"}


async def test_reconfigure_to_a_local_server_moves_the_unique_id(
    hass: HomeAssistant, fake_client: MagicMock, loaded: MockConfigEntry
) -> None:
    result = await open_reconfigure(hass, loaded)
    result = await answer(hass, result, typed("local_clef", "", url=LOCAL_URL))
    assert result["reason"] == "reconfigure_successful"
    assert loaded.data == ENTRY_DATA["local_clef"]
    assert loaded.unique_id == entry_unique_id(LOCAL_URL, "")


async def test_reconfigure_probe_failure_stays_on_the_form(
    hass: HomeAssistant, fake_client: MagicMock, loaded: MockConfigEntry
) -> None:
    result = await open_reconfigure(hass, loaded)
    fake_client.ask.side_effect = JevConnectionError("timed out")
    result = await answer(hass, result, typed(url="https://198.51.100.70"))
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_reconfigure_to_cloudflare_asks_the_account_then_probes(
    hass: HomeAssistant, fake_client: MagicMock, loaded: MockConfigEntry
) -> None:
    result = await open_reconfigure(hass, loaded)
    result = await answer(hass, result, typed("cloudflare", "cf-fresh-token"))
    assert result["step_id"] == "account_id"

    fake_client.ask.side_effect = JevValidationError("5007: no such model")
    result = await answer(hass, result, {CONF_ACCOUNT_ID: ACCOUNT_ID})
    assert result["errors"] == {"base": "model_refused"}

    fake_client.ask.side_effect = None
    result = await answer(hass, result, {CONF_ACCOUNT_ID: ACCOUNT_ID})
    assert result["reason"] == "reconfigure_successful"
    assert loaded.data == {**ENTRY_DATA["cloudflare"], CONF_API_KEY: "cf-fresh-token"}


@pytest.mark.parametrize("provider_key", ["cloudflare"], indirect=True)
async def test_reconfigure_cloudflare_offers_the_stored_account(
    hass: HomeAssistant, fake_client: MagicMock, loaded: MockConfigEntry
) -> None:
    result = await open_reconfigure(hass, loaded)
    result = await answer(hass, result, typed("cloudflare", "", model="clef"))
    assert result["step_id"] == "account_id"
    field = next(iter(result["data_schema"].schema))
    assert field.description["suggested_value"] == ACCOUNT_ID
    result = await answer(hass, result, {CONF_ACCOUNT_ID: ACCOUNT_ID})
    assert result["reason"] == "reconfigure_successful"
    assert loaded.data[CONF_MODEL] == "clef"
    # Same account, same key: the unique id does not follow the model.
    assert loaded.unique_id == _unique_id(ENTRY_DATA["cloudflare"])


async def test_reconfigure_onto_another_entrys_server_aborts(
    hass: HomeAssistant, fake_client: MagicMock, loaded: MockConfigEntry
) -> None:
    MockConfigEntry(
        domain=DOMAIN,
        data=ENTRY_DATA["openrouter"],
        unique_id=_unique_id(ENTRY_DATA["openrouter"]),
    ).add_to_hass(hass)
    result = await open_reconfigure(hass, loaded)
    result = await answer(hass, result, typed("openrouter"))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# --- options ---


@pytest.mark.parametrize(
    ("provider_key", "price"),
    [("typesafe", 0.042), ("cloudflare", 0.038), ("local_clef", 0.0)],
    indirect=["provider_key"],
)
async def test_options_default_to_no_budget_and_the_providers_price(
    hass: HomeAssistant, fake_client: MagicMock, loaded: MockConfigEntry, price: float
) -> None:
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    assert result["type"] is FlowResultType.FORM
    defaults = {key.schema: key.default() for key in result["data_schema"].schema}
    assert defaults == {CONF_TOKEN_BUDGET: 0, CONF_TOKEN_PRICE: price, CONF_MAX_IMAGES: 4}


async def test_options_submitted_are_stored(
    hass: HomeAssistant, fake_client: MagicMock, loaded: MockConfigEntry
) -> None:
    chosen = {CONF_TOKEN_BUDGET: 75_000, CONF_TOKEN_PRICE: 0.31, CONF_MAX_IMAGES: 3}
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], chosen)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert loaded.options == chosen


async def test_options_refuse_more_pictures_than_the_cap(
    hass: HomeAssistant, fake_client: MagicMock, loaded: MockConfigEntry
) -> None:
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(result["flow_id"], {CONF_MAX_IMAGES: 6})
