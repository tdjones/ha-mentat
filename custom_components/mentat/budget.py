"""One config entry's day of billed input tokens.

A `Ledger` holds what the server billed since local midnight: how many requests
and how many input tokens. The totals go to a store, so a reload or a restart in
the afternoon keeps the morning's spend, and they reset when the day changes.
Before a request goes out, the ledger turns the body size into a padded token
estimate and says whether it fits. The bytes-per-token ratio behind the estimate
starts as a guess and is relearned from every call whose body was billed enough
tokens to trust. A request holds a reservation while it is in flight, so two
askers that each fit alone cannot both be sent when only one fits.

The ledger refuses nothing by itself. Callers ask `fits()` and raise their own
error, which names what they were about to ask. A budget of 0 means no limit.
When the day's tokens reach the budget, the over-budget flag goes up and one
repair issue per entry goes up with it; both come down together on a new day or
when a reload finds the budget raised. The ledger never sends a request.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import date
from math import ceil
from types import TracebackType
from typing import Any, Protocol

from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    BUDGET_ESTIMATE_MARGIN,
    COLD_START_BYTES_PER_TOKEN,
    DOMAIN,
    ISSUE_OVER_BUDGET,
    MIN_MEASURED_BODY_TOKENS,
    REQUEST_OVERHEAD_TOKENS,
    STORE_SAVE_DELAY_SECONDS,
)


def over_budget_issue_id(entry_id: str) -> str:
    """The id of an entry's over-budget repair issue."""
    return f"{ISSUE_OVER_BUDGET}_{entry_id}"


class Ledger:
    """What one entry was billed today, and what it may still send."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        *,
        day: date,
        budget: int = 0,
        token_price: float = 0.0,
        store: Store[dict[str, Any]] | None = None,
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._keeper: _Keeper = store if store is not None else _Forgetful()
        self._listeners: dict[object, Callable[[], None]] = {}
        self.day = day
        self.requests = 0
        self.tokens = 0
        self.budget = budget
        # US dollars per million input tokens.
        self.token_price = token_price
        self.over_budget = False
        # Not stored: a restart relearns it from the first big enough call, and a
        # ratio kept from another model or server would mislead until then.
        self.bytes_per_token = COLD_START_BYTES_PER_TOKEN
        self.reserved = 0

    @property
    def issue_id(self) -> str:
        return over_budget_issue_id(self._entry_id)

    @property
    def spend(self) -> float:
        """Today's tokens at the configured price, in US dollars."""
        return self.tokens * self.token_price / 1_000_000

    @property
    def spoken_for(self) -> int:
        """Tokens billed today plus those held by requests still in flight."""
        return self.tokens + self.reserved

    def snapshot(self) -> dict[str, Any]:
        return {"day": self.day.isoformat(), "requests": self.requests, "tokens": self.tokens}

    def adopt(self, stored: Any) -> None:
        """Take today's totals from the store, then set the flag from them.

        The repair issue outlives a reload in the registry. Setting the flag here,
        either way, is what clears it once the options raise the budget.
        """
        totals = _totals_for(self.day, stored)
        if totals is not None:
            self.requests, self.tokens = totals
        self.mark_over_budget(self._spent(), used=self.tokens)

    def roll_over(self, today: date) -> None:
        """Start a new day of totals, and tell the listeners, once per day."""
        if today == self.day:
            return
        self.day = today
        self.requests = 0
        self.tokens = 0
        self.mark_over_budget(False)
        self._save_soon()
        self.notify()

    def record(self, tokens: int, body_bytes: int | None = None, picture_tokens: int = 0) -> None:
        """Count one billed request, and relearn the ratio from its body.

        Leave `body_bytes` out for a request that is mostly fixed overhead, such
        as the setup probe: its ratio would make every later estimate too high.
        """
        self.roll_over(dt_util.now().date())
        self.requests += 1
        self.tokens += tokens
        body_tokens = tokens - REQUEST_OVERHEAD_TOKENS - picture_tokens
        if body_bytes is not None and body_tokens >= MIN_MEASURED_BODY_TOKENS:
            self.bytes_per_token = body_bytes / body_tokens
        if self._spent() and not self.over_budget:
            self.mark_over_budget(True, used=self.tokens)
        self._save_soon()

    async def async_flush(self) -> None:
        """Called at unload, so totals still waiting out the save delay are kept."""
        await self._keeper.async_save(self.snapshot())

    def estimate(self, body_bytes: int, picture_tokens: int = 0) -> int:
        """Tokens a body of this size should cost, padded and rounded up."""
        body_tokens = body_bytes / self.bytes_per_token
        return ceil(
            (REQUEST_OVERHEAD_TOKENS + body_tokens + picture_tokens) * BUDGET_ESTIMATE_MARGIN
        )

    def fits(self, estimate: int) -> bool:
        """Whether a request of this estimate may go out. Landing on the budget fits."""
        return self.budget <= 0 or self.spoken_for + estimate <= self.budget

    def has_room(self) -> bool:
        """Whether the smallest possible request still fits."""
        return self.fits(self.estimate(0))

    def remaining(self) -> int:
        """Tokens left after what is spoken for, or 0 when there is no budget."""
        if self.budget <= 0:
            return 0
        return max(self.budget - self.spoken_for, 0)

    def reserve(self, estimate: int) -> AbstractContextManager[None]:
        """Hold an estimate against the budget while its request is in flight."""
        return _Hold(self, estimate)

    def mark_over_budget(self, over: bool, used: int = 0) -> None:
        """Move the flag and the entry's repair issue together."""
        self.over_budget = over
        if not over:
            ir.async_delete_issue(self._hass, DOMAIN, self.issue_id)
            return
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            self.issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_OVER_BUDGET,
            translation_placeholders={"budget": str(self.budget), "used": str(used)},
        )

    def async_add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Call `listener` on every notify. The returned remover is safe to call twice."""
        token = object()
        self._listeners[token] = listener

        def remove() -> None:
            self._listeners.pop(token, None)

        return remove

    def notify(self) -> None:
        for listener in list(self._listeners.values()):
            listener()

    def _spent(self) -> bool:
        return 0 < self.budget <= self.tokens

    def _save_soon(self) -> None:
        self._keeper.async_delay_save(self.snapshot, STORE_SAVE_DELAY_SECONDS)


class _Keeper(Protocol):
    """The two Store methods the ledger uses."""

    async def async_save(self, data: dict[str, Any]) -> None: ...

    def async_delay_save(
        self, data_func: Callable[[], dict[str, Any]], delay: float = 0
    ) -> None: ...


class _Forgetful:
    """Stands in for the store when there is none, as in tests. Keeps nothing."""

    async def async_save(self, data: dict[str, Any]) -> None:
        return

    def async_delay_save(self, data_func: Callable[[], dict[str, Any]], delay: float = 0) -> None:
        return


class _Hold(AbstractContextManager[None]):
    """One in-flight reservation. Released on the way out, even by an exception."""

    def __init__(self, ledger: Ledger, estimate: int) -> None:
        self._ledger = ledger
        self._estimate = estimate

    def __enter__(self) -> None:
        self._ledger.reserved += self._estimate

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._ledger.reserved -= self._estimate


def _totals_for(day: date, stored: Any) -> tuple[int, int] | None:
    """Requests and tokens from stored data, when it is readable and from `day`."""
    if not isinstance(stored, Mapping):
        return None
    try:
        if date.fromisoformat(stored["day"]) != day:
            return None
        return int(stored.get("requests", 0)), int(stored.get("tokens", 0))
    except (KeyError, TypeError, ValueError):
        return None
