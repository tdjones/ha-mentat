"""The ledger: a day of billed tokens, the estimate before a call, and the issue."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.mentat.budget import Ledger
from custom_components.mentat.const import (
    BUDGET_ESTIMATE_MARGIN,
    COLD_START_BYTES_PER_TOKEN,
    DOMAIN,
    ISSUE_OVER_BUDGET,
    MIN_MEASURED_BODY_TOKENS,
    REQUEST_OVERHEAD_TOKENS,
    STORE_SAVE_DELAY_SECONDS,
)

ENTRY = "ledger_entry"
STORE_KEY = f"{DOMAIN}.{ENTRY}.ledger"


def _today() -> date:
    return dt_util.now().date()


def _ledger(hass: HomeAssistant, **kwargs: Any) -> Ledger:
    kwargs.setdefault("day", _today())
    return Ledger(hass, ENTRY, **kwargs)


def _issue(hass: HomeAssistant, ledger: Ledger) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, ledger.issue_id)


async def test_ledger_counts_each_recorded_request(hass: HomeAssistant) -> None:
    ledger = _ledger(hass)
    ledger.record(730)
    ledger.record(95)
    assert (ledger.requests, ledger.tokens) == (2, 825)


async def test_ledger_learns_the_ratio_from_a_large_body(hass: HomeAssistant) -> None:
    ledger = _ledger(hass)
    ledger.record(REQUEST_OVERHEAD_TOKENS + 800, body_bytes=2_400)
    assert ledger.bytes_per_token == 3.0


async def test_ledger_takes_picture_tokens_off_before_learning(hass: HomeAssistant) -> None:
    ledger = _ledger(hass)
    ledger.record(REQUEST_OVERHEAD_TOKENS + 1_105 + 600, body_bytes=1_500, picture_tokens=1_105)
    assert ledger.bytes_per_token == 2.5


async def test_ledger_learns_from_a_body_billed_exactly_the_minimum(hass: HomeAssistant) -> None:
    ledger = _ledger(hass)
    ledger.record(
        REQUEST_OVERHEAD_TOKENS + MIN_MEASURED_BODY_TOKENS, body_bytes=4 * MIN_MEASURED_BODY_TOKENS
    )
    assert ledger.bytes_per_token == 4.0


@pytest.mark.parametrize(
    ("tokens", "body_bytes"),
    [
        (REQUEST_OVERHEAD_TOKENS + MIN_MEASURED_BODY_TOKENS - 1, 9_000),
        (12, 60),
        (REQUEST_OVERHEAD_TOKENS + 900, None),
    ],
)
async def test_ledger_keeps_the_cold_ratio_after_a_small_or_unmeasured_body(
    hass: HomeAssistant, tokens: int, body_bytes: int | None
) -> None:
    ledger = _ledger(hass)
    ledger.record(tokens, body_bytes=body_bytes)
    assert ledger.bytes_per_token == COLD_START_BYTES_PER_TOKEN


async def test_ledger_record_on_a_later_day_starts_the_day_over(hass: HomeAssistant) -> None:
    ledger = _ledger(hass, day=_today() - timedelta(days=1))
    ledger.requests, ledger.tokens = 5, 4_400
    ledger.record(310)
    assert (ledger.day, ledger.requests, ledger.tokens) == (_today(), 1, 310)


async def test_ledger_estimate_pads_the_body_and_pictures(hass: HomeAssistant) -> None:
    ledger = _ledger(hass)
    ledger.bytes_per_token = 3.0
    exact = (REQUEST_OVERHEAD_TOKENS + 1_000 / 3.0 + 85) * BUDGET_ESTIMATE_MARGIN
    estimate = ledger.estimate(1_000, picture_tokens=85)
    assert isinstance(estimate, int)
    assert exact <= estimate < exact + 1


async def test_ledger_estimate_of_an_empty_body_is_the_padded_overhead(
    hass: HomeAssistant,
) -> None:
    exact = REQUEST_OVERHEAD_TOKENS * BUDGET_ESTIMATE_MARGIN
    assert exact <= _ledger(hass).estimate(0) < exact + 1


async def test_ledger_without_a_budget_fits_anything(hass: HomeAssistant) -> None:
    ledger = _ledger(hass)
    ledger.record(7 * 10**8)
    assert ledger.fits(10**9)
    assert ledger.has_room()
    assert ledger.remaining() == 0
    assert not ledger.over_budget
    assert _issue(hass, ledger) is None


async def test_ledger_counts_reservations_against_the_budget(hass: HomeAssistant) -> None:
    ledger = _ledger(hass, budget=2_000)
    ledger.tokens = 1_150
    assert ledger.remaining() == 850
    with ledger.reserve(500):
        assert ledger.spoken_for == 1_650
        assert ledger.remaining() == 350
        assert ledger.fits(350)
        assert not ledger.fits(351)
    assert ledger.reserved == 0
    assert ledger.remaining() == 850


async def test_ledger_reservations_add_up_while_both_are_held(hass: HomeAssistant) -> None:
    ledger = _ledger(hass, budget=1_000)
    with ledger.reserve(400), ledger.reserve(300):
        assert ledger.reserved == 700
        assert not ledger.fits(301)
    assert ledger.reserved == 0


async def test_ledger_releases_a_reservation_when_the_request_fails(
    hass: HomeAssistant,
) -> None:
    ledger = _ledger(hass, budget=3_000)
    with pytest.raises(ConnectionError), ledger.reserve(1_200):
        raise ConnectionError("no route to host")
    assert ledger.reserved == 0


async def test_ledger_has_room_only_for_a_minimal_request(hass: HomeAssistant) -> None:
    ledger = _ledger(hass, budget=6_000)
    ledger.tokens = 6_000 - ledger.estimate(0)
    assert ledger.has_room()
    ledger.tokens += 1
    assert not ledger.has_room()


async def test_ledger_spend_is_tokens_times_the_price_per_million(hass: HomeAssistant) -> None:
    ledger = _ledger(hass, token_price=0.06)
    ledger.tokens = 3_500_000
    assert ledger.spend == pytest.approx(0.21)


@pytest.mark.parametrize(
    "stored",
    [
        None,
        {},
        ["2026-10-30", 4, 900],
        {"requests": 6, "tokens": 700},
        {"day": "the thirtieth", "requests": 6},
        {"day": "2026-10-30", "tokens": "plenty"},
        {"day": 20261030, "requests": 6},
    ],
)
async def test_ledger_adopt_keeps_zero_for_unreadable_data(
    hass: HomeAssistant, stored: Any
) -> None:
    ledger = _ledger(hass, day=date(2026, 10, 30))
    ledger.adopt(stored)
    assert (ledger.requests, ledger.tokens) == (0, 0)


async def test_ledger_adopt_ignores_another_day(hass: HomeAssistant) -> None:
    ledger = _ledger(hass, day=date(2026, 10, 30))
    ledger.adopt({"day": "2026-10-29", "requests": 11, "tokens": 5_200})
    assert (ledger.requests, ledger.tokens) == (0, 0)


async def test_ledger_snapshot_round_trips_through_adopt(hass: HomeAssistant) -> None:
    first = _ledger(hass, day=date(2026, 10, 30))
    first.requests, first.tokens = 6, 2_718
    second = _ledger(hass, day=date(2026, 10, 30))
    second.adopt(first.snapshot())
    assert (second.requests, second.tokens) == (6, 2_718)


async def test_ledger_issue_id_carries_the_entry_id(hass: HomeAssistant) -> None:
    assert Ledger(hass, "e9f1", day=_today()).issue_id == f"{ISSUE_OVER_BUDGET}_e9f1"


async def test_ledger_raises_one_issue_when_a_record_crosses_the_budget(
    hass: HomeAssistant,
) -> None:
    ledger = _ledger(hass, budget=800)
    ledger.record(500)
    assert not ledger.over_budget
    assert _issue(hass, ledger) is None

    ledger.record(450)
    assert ledger.over_budget
    issue = _issue(hass, ledger)
    assert issue is not None
    assert issue.translation_key == "over_budget"
    assert issue.severity is ir.IssueSeverity.WARNING
    assert not issue.is_fixable
    assert issue.translation_placeholders == {"budget": "800", "used": "950"}


async def test_ledger_new_day_lowers_the_flag_and_deletes_the_issue(
    hass: HomeAssistant,
) -> None:
    ledger = _ledger(hass, budget=800)
    ledger.record(1_300)
    heard: list[int] = []
    ledger.async_add_listener(lambda: heard.append(ledger.requests))

    tomorrow = _today() + timedelta(days=1)
    ledger.roll_over(tomorrow)
    assert not ledger.over_budget
    assert _issue(hass, ledger) is None
    assert (ledger.day, ledger.requests, ledger.tokens) == (tomorrow, 0, 0)
    assert heard == [0]

    ledger.requests = 1
    ledger.roll_over(tomorrow)
    assert ledger.requests == 1
    assert heard == [0]


async def test_ledger_adopt_raises_the_issue_for_a_spent_day(hass: HomeAssistant) -> None:
    stored = {"day": _today().isoformat(), "requests": 9, "tokens": 1_750}
    tight = _ledger(hass, budget=1_500)
    tight.adopt(stored)
    assert tight.over_budget
    issue = _issue(hass, tight)
    assert issue is not None
    assert issue.translation_placeholders == {"budget": "1500", "used": "1750"}


async def test_ledger_adopt_under_a_raised_budget_deletes_the_issue(
    hass: HomeAssistant,
) -> None:
    stored = {"day": _today().isoformat(), "requests": 9, "tokens": 1_750}
    _ledger(hass, budget=1_500).adopt(stored)
    roomy = _ledger(hass, budget=15_000)
    roomy.adopt(stored)
    assert not roomy.over_budget
    assert _issue(hass, roomy) is None


async def test_ledger_flush_writes_the_totals_at_once(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    ledger = _ledger(hass, store=Store(hass, 1, STORE_KEY))
    ledger.record(615)
    await ledger.async_flush()
    assert hass_storage[STORE_KEY]["data"] == ledger.snapshot()


async def test_ledger_saves_a_record_after_the_delay(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    ledger = _ledger(hass, store=Store(hass, 1, STORE_KEY))
    ledger.record(615)
    await hass.async_block_till_done()
    assert STORE_KEY not in hass_storage

    later = dt_util.utcnow() + timedelta(seconds=STORE_SAVE_DELAY_SECONDS + 1)
    async_fire_time_changed(hass, later)
    await hass.async_block_till_done()
    copy = _ledger(hass)
    copy.adopt(hass_storage[STORE_KEY]["data"])
    assert (copy.requests, copy.tokens) == (1, 615)


async def test_ledger_flush_without_a_store_does_nothing(hass: HomeAssistant) -> None:
    await _ledger(hass).async_flush()


async def test_ledger_listener_stops_hearing_once_removed(hass: HomeAssistant) -> None:
    ledger = _ledger(hass)
    heard: list[str] = []
    remove = ledger.async_add_listener(lambda: heard.append("told"))
    ledger.notify()
    remove()
    remove()
    ledger.notify()
    assert heard == ["told"]
