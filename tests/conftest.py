"""Fixtures. Nothing here reaches a server: every client is replaced or mocked."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from unittest.mock import DEFAULT, AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_API_KEY, CONF_MODEL, CONF_URL
from homeassistant.core import HomeAssistant, State
from homeassistant.setup import async_setup_component
from jevclient import Answer, ChoiceAnswer, JevResponse, NoulAnswer, ScoreAnswer, Usage
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mentat.const import (
    CONF_ACCOUNT_ID,
    CONF_DIALECT,
    CONF_EXTRA,
    CONF_IMAGE_FORMAT,
    CONF_PROVIDER,
    DOMAIN,
)
from custom_components.mentat.providers import Dialect

API_KEY = "fixture-key-7f3a"
ACCOUNT_ID = "0123456789abcdef0123456789abcdef"
# Documentation range (RFC 5737), never a real host.
LOCAL_URL = "http://192.0.2.10:8000"

# Billed tokens for the setup probe and for any other request, kept apart so a
# test can tell which one was counted.
PROBE_TOKENS = 57
ANSWER_TOKENS = 418
LATENCY_MS = 186.0
BODY_BYTES = 1_200

# Entry data per provider, the way the config flow writes it.
ENTRY_DATA: dict[str, dict[str, Any]] = {
    "typesafe": {
        CONF_PROVIDER: "typesafe",
        CONF_API_KEY: API_KEY,
        CONF_URL: "https://api.typesafe.ai",
        CONF_MODEL: "jev-latest",
        CONF_DIALECT: "typesafe",
        CONF_IMAGE_FORMAT: "assets",
    },
    "openrouter": {
        CONF_PROVIDER: "openrouter",
        CONF_API_KEY: API_KEY,
        CONF_URL: "https://openrouter.ai/api",
        CONF_MODEL: "~typesafe/jev-latest",
        CONF_DIALECT: "typesafe",
        CONF_IMAGE_FORMAT: "assets",
    },
    "cloudflare": {
        CONF_PROVIDER: "cloudflare",
        CONF_API_KEY: API_KEY,
        CONF_URL: (
            "https://api.cloudflare.com/client/v4/accounts/{account_id}"
            "/ai/run/@cf/cloudflare/{model}"
        ),
        CONF_MODEL: "clef-flash",
        CONF_DIALECT: "clef",
        CONF_IMAGE_FORMAT: "base64",
        CONF_ACCOUNT_ID: ACCOUNT_ID,
    },
    "local_clef": {
        CONF_PROVIDER: "local_clef",
        CONF_API_KEY: "",
        CONF_URL: LOCAL_URL,
        CONF_MODEL: "clef-flash",
        CONF_DIALECT: "clef",
        CONF_IMAGE_FORMAT: "data_url",
        CONF_EXTRA: {"truncate": False},
    },
    "generic": {
        CONF_PROVIDER: "generic",
        CONF_API_KEY: "",
        CONF_URL: "http://192.0.2.20:9000",
        CONF_MODEL: "jev-latest",
        CONF_DIALECT: "typesafe",
        CONF_IMAGE_FORMAT: "assets",
    },
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Without this the custom component is never loaded."""


@pytest.fixture(autouse=True)
async def homeassistant_component(hass: HomeAssistant) -> None:
    """Set up the `homeassistant` component, which a real instance always has.

    The AI Task platform needs conversation, and conversation reads the exposed
    entities that this component sets up. Without it every entry fails to load
    its ai_task platform.
    """
    assert await async_setup_component(hass, "homeassistant", {})


def build_response(
    *, model: str = "jev-1.14.0", input_tokens: int = ANSWER_TOKENS, **answers: Answer
) -> JevResponse:
    return JevResponse(
        model=model,
        answers=dict(answers),
        usage=Usage(input_tokens=input_tokens, output_tokens=3),
        latency_ms=LATENCY_MS,
    )


def _probe_or_default(state: Any, questions: dict[str, Any], *args: Any, **kwargs: Any):
    """Answer the setup probe the way a server does, and anything else as set.

    The probe is billed, so it lands in the day's totals at PROBE_TOKENS.
    """
    if "probe" in questions:
        return build_response(input_tokens=PROBE_TOKENS, probe=NoulAnswer(noul=0.97))
    return DEFAULT


@pytest.fixture
def fake_client() -> Iterator[MagicMock]:
    """Replace build_client in both places it is called.

    `built_by_setup` and `built_by_flow` are the two patches, so a test can check
    what a client was pointed at as well as what it was asked.
    """
    client = MagicMock()
    client.dialect = Dialect.TYPESAFE
    client.ask = AsyncMock(
        return_value=build_response(answer=NoulAnswer(noul=0.81)),
        side_effect=_probe_or_default,
    )
    client.async_close = AsyncMock()
    # A body of 1,200 bytes is about 545 tokens at the cold-start ratio, so a
    # text call fits a budget of a few thousand and a single camera frame does not.
    client.body_size.return_value = BODY_BYTES
    with (
        patch("custom_components.mentat.build_client", return_value=client) as setup,
        patch("custom_components.mentat.config_flow.build_client", return_value=client) as flow,
    ):
        client.built_by_setup = setup
        client.built_by_flow = flow
        yield client


@pytest.fixture(params=["typesafe"])
def provider_key(request: pytest.FixtureRequest) -> str:
    """Override with indirect parametrisation to run a test per provider."""
    return str(request.param)


@pytest.fixture
def entry(provider_key: str) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=provider_key,
        data=dict(ENTRY_DATA[provider_key]),
        options={},
        unique_id=f"unique-{provider_key}",
        version=1,
        minor_version=1,
    )


@pytest.fixture
async def loaded(
    hass: HomeAssistant, fake_client: MagicMock, entry: MockConfigEntry
) -> MockConfigEntry:
    """`entry`, set up with no watches."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


# A YAML watch the way a user writes one: one noul with a cutoff, one choice
# and one score, all about one washer.
WASHER = "sensor.washer_power"
WATCH: dict[str, Any] = {
    "name": "Laundry",
    "scan_interval": 300,
    "entities": [WASHER],
    "questions": [
        {
            "name": "Laundry forgotten",
            "type": "noul",
            "instructions": "Is the laundry finished but still in the machine?",
            "cutoff": 0.7,
        },
        {
            "name": "Programme",
            "type": "choice",
            "instructions": "Which programme is running?",
            "criteria": {"wash": "Drum turning in water", "spin": None, "idle": None},
        },
        {
            "name": "Urgency",
            "type": "score",
            "instructions": "How soon must someone empty it?",
            "criteria": ["Not yet", "Today", "Now"],
        },
    ],
}
NOUL_KEY = "laundry_laundry_forgotten"
CHOICE_KEY = "laundry_programme"
SCORE_KEY = "laundry_urgency"


def laundry_answers() -> dict[str, Answer]:
    """An answer for every question in WATCH."""
    return {
        NOUL_KEY: NoulAnswer(noul=0.81),
        CHOICE_KEY: ChoiceAnswer(
            choice="spin",
            probabilities={"wash": 0.1, "spin": 0.85, "idle": 0.05},
            confidence=0.8,
        ),
        SCORE_KEY: ScoreAnswer(
            score=1.4,
            legend={"0": "Not yet", "1": "Today", "2": "Now"},
            probabilities={"0": 0.1, "1": 0.4, "2": 0.5},
            confidence=0.6,
        ),
    }


type LoadYaml = Callable[..., Awaitable[MockConfigEntry]]


@pytest.fixture
def yaml_watch(hass: HomeAssistant, fake_client: MagicMock, entry: MockConfigEntry) -> LoadYaml:
    """Load YAML watches, then the entry that owns them, and return that entry.

    `await yaml_watch()` loads WATCH under `entry`. Pass a list to load other
    watches, and `owner=` to load them under another entry.
    """

    async def load(
        watches: list[dict[str, Any]] | None = None, *, owner: MockConfigEntry | None = None
    ) -> MockConfigEntry:
        owner = owner or entry
        hass.states.async_set(WASHER, "1.2", {"friendly_name": "Washer power"})
        assert await async_setup_component(
            hass, DOMAIN, {DOMAIN: [WATCH] if watches is None else watches}
        )
        if owner.entry_id not in {known.entry_id for known in hass.config_entries.async_entries()}:
            owner.add_to_hass(hass)
        assert await hass.config_entries.async_setup(owner.entry_id)
        await hass.async_block_till_done()
        return owner

    return load


@dataclass
class FakeRecorder:
    """What the window's backfill reads, and how it was asked.

    `history` maps an entity id to the states the recorder would return for it.
    `calls` keeps the arguments of every query, one query reading every entity.
    `during` runs inside the query, as a change landing while the recorder is
    read would.
    """

    history: dict[str, list[State]] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)
    error: Exception | None = None
    during: Callable[[], None] | None = None

    def query(
        self,
        hass: HomeAssistant,
        start: datetime,
        end: datetime,
        entity_ids: list[str],
        **kwargs: Any,
    ) -> dict[str, list[State]]:
        self.calls.append({"start": start, "end": end, "entity_ids": entity_ids, **kwargs})
        if self.error is not None:
            raise self.error
        if self.during is not None:
            self.during()
        return {entity_id: self.history.get(entity_id, []) for entity_id in entity_ids}


@pytest.fixture
def mock_recorder(hass: HomeAssistant) -> Iterator[FakeRecorder]:
    """A recorder that answers from memory; the real one needs a database.

    Request it after any fixture that loads an entry, so only the window sees it.
    """
    fake = FakeRecorder()
    instance = MagicMock()

    async def run(job: Callable[[], Any]) -> Any:
        return job()

    instance.async_add_executor_job = AsyncMock(side_effect=run)
    hass.config.components.add("recorder")
    with (
        patch("custom_components.mentat.window.get_instance", return_value=instance),
        patch(
            "homeassistant.components.recorder.history.get_significant_states",
            side_effect=fake.query,
        ),
    ):
        yield fake
    if "recorder" in hass.config.components:
        hass.config.components.remove("recorder")
