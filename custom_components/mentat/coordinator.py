"""What an entry owns while loaded, and one coordinator per watch.

A coordinator asks one watch's questions in one request. An evaluation renders
the note, builds the state, checks the day's budget, sends, and records what was
sent. Evaluations of one watch run one at a time, and a watch whose answers
nobody reads is not asked at all, because every request is billed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import (
    CALLBACK_TYPE,
    CoreState,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ServiceValidationError,
    TemplateError,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.target import (
    TargetStateChangedData,
    async_track_target_selector_state_change_event,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from jevclient import (
    Answer,
    JevAuthError,
    JevError,
    JevRateLimitError,
    JevResponse,
    Question,
)

from .budget import Ledger
from .client import DecisionClient
from .const import (
    ATTR_CONFIDENT,
    ATTR_EVENT_COUNT,
    ATTR_HELD,
    ATTR_LAST_EVALUATED,
    ATTR_WINDOW_END,
    ATTR_WINDOW_START,
    DOMAIN,
    EVALUATE_INTERVAL,
    EVALUATE_ON_EVENT,
    EVENT_DECISION,
    TRIGGER_DEBOUNCE_SECONDS,
    UID_BYTES,
    UID_LATENCY,
    UID_VERDICT,
    UID_WATCH,
    YAML_ID_SCOPE,
)
from .judge import Verdict, confident, verdict
from .providers import Provider
from .readings import async_state, is_mentat_entity
from .watches import WatchConfig, answer_as_dict
from .window import EventWindow, async_build_window_state

_LOGGER = logging.getLogger(__name__)


def unique_id(scope: str, *parts: str) -> str:
    """An entity's unique id: its scope, then a question or watch key, then a UID_* suffix.

    The platforms build their ids here and nobody_reads() looks the same ids up,
    so the two cannot drift apart. Keys are slugs and entry ids are ULIDs, and
    neither holds a colon, so joining on one keeps every kind of id apart: a
    question named "latency" cannot take its watch's latency diagnostic's id.
    """
    return ":".join((scope, *parts))


@dataclass(frozen=True, slots=True)
class WindowSpan:
    """The stretch of a window's log that one answer judged."""

    start: datetime
    end: datetime
    events: int
    # When the answer came back, which is later than `end` by the latency.
    evaluated: datetime

    def as_attributes(self) -> dict[str, Any]:
        return {
            ATTR_WINDOW_START: self.start.isoformat(),
            ATTR_WINDOW_END: self.end.isoformat(),
            ATTR_EVENT_COUNT: self.events,
            ATTR_LAST_EVALUATED: self.evaluated.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class Exchange:
    """What the last answered request of a watch sent, and how long it took.

    A failed evaluation leaves the previous one standing, so the diagnostic
    sensors keep showing the last request that worked.
    """

    state: Any
    body_bytes: int
    latency_ms: float
    window: WindowSpan | None = None


@dataclass(kw_only=True, slots=True)
class MentatRuntimeData:
    """Everything a config entry holds while it is loaded."""

    provider: Provider
    client: DecisionClient
    # The model the entry asks for. The client knows it as well, but the budget
    # estimate measures the body the client will post, and the body names it.
    model: str
    ledger: Ledger
    # What the server said it ran. For "jev-latest" this is the release.
    model_version: str | None = None
    # Keyed by watch key.
    coordinators: dict[str, MentatCoordinator] = field(default_factory=dict)

    async def async_stop(self) -> None:
        """Stop every watch listening, write the day's totals and close the client."""
        for coordinator in self.coordinators.values():
            coordinator.async_shutdown_triggers()
        await self.ledger.async_flush()
        await self.client.async_close()


@callback
def _keep_schedule() -> None:
    """A listener with nothing to update.

    DataUpdateCoordinator runs its schedule only while a listener is registered.
    An automation that triggers on mentat_decision registers none, so a watch
    that fires the event adds this one.
    """


class MentatCoordinator(DataUpdateCoordinator[dict[str, Answer]]):
    """Evaluates one watch: every question in one request, against one state."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        runtime: MentatRuntimeData,
        watch: WatchConfig,
    ) -> None:
        window = watch.window
        # A window asked only after its log settles has no schedule.
        scheduled = window is None or window.evaluate != EVALUATE_ON_EVENT
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {watch.name}",
            update_interval=timedelta(seconds=watch.scan_interval) if scheduled else None,
        )
        self.runtime = runtime
        self.watch_config = watch
        # What the watch's unique ids start with. A YAML watch's keep theirs when
        # another entry takes the YAML watches over.
        self.id_scope = YAML_ID_SCOPE if watch.from_yaml else entry.entry_id
        self.window: EventWindow | None = None
        self.last: Exchange | None = None
        # Each verdict as the last answer left it, by question key. A verdict
        # held between release and cutoff reads the one before. Nothing is kept
        # across a restart, as no answer is.
        self.verdicts: dict[str, Verdict] = {}
        # Unsubscribe callbacks, run last first at shutdown.
        self._stops: list[CALLBACK_TYPE] = []
        self._debouncer: Debouncer[None] | None = None
        # True from the first failure of an outage until the next answer, so a
        # watch asked every 30 s does not log 2,880 errors a day.
        self._server_down = False
        # One evaluation at a time: a request that arrives during one sets the
        # flag, and any number of them make one more evaluation after it.
        self._evaluating = False
        self._evaluate_again = False
        # True from setup until Home Assistant has started, while nothing is asked.
        self._waiting_for_start = False

    @property
    def window_span(self) -> WindowSpan | None:
        """The window the last answer judged. None for a plain watch or before an answer."""
        return None if self.last is None else self.last.window

    async def async_request_evaluation(self) -> None:
        """Evaluate now, or once more after the evaluation already running.

        Every request to evaluate comes through here: setup, a trigger, a
        settled window and the schedule. Two evaluations of one watch never
        overlap, so their answers land in the order they were asked, and the
        requests that arrive during one are folded into a single evaluation
        after it rather than lost or run once each.
        """
        if self._waiting_for_start:
            return
        if self._evaluating:
            self._evaluate_again = True
            return
        self._evaluating = True
        try:
            await self.async_refresh()
            while self._evaluate_again:
                self._evaluate_again = False
                await self.async_refresh()
        finally:
            self._evaluating = False

    async def async_first_evaluation(self) -> None:
        """Ask for the first time, now or once Home Assistant has started.

        While it starts, other integrations are still adding their entities. A
        watch asked then is refused for having nothing to read, or worse, is
        billed for an answer about the part of the house that has loaded so far.
        So nothing wakes it until then, and the entities wait unavailable.
        """
        if self.hass.state is CoreState.running:
            if not self.nobody_reads():
                await self.async_request_evaluation()
            return
        self._waiting_for_start = True
        self._stops.append(async_at_started(self.hass, self._started))

    @callback
    def _started(self, _hass: HomeAssistant) -> None:
        self._waiting_for_start = False
        if not self.nobody_reads():
            self.async_schedule_evaluation()

    @callback
    def async_schedule_evaluation(self) -> None:
        """async_request_evaluation() from a callback, without waiting for it.

        A debouncer that awaited the evaluation would hold its own lock while it
        ran, and HA's Debouncer drops a call that comes due while it is locked.
        """
        self.config_entry.async_create_background_task(
            self.hass,
            self.async_request_evaluation(),
            name=f"{self.name} evaluation",
            eager_start=True,
        )

    async def _handle_refresh_interval(self, _now: datetime | None = None) -> None:
        """The schedule's tick, folded in with every other request.

        DataUpdateCoordinator would queue it on its lock behind a running
        evaluation and then run it as well; through async_request_evaluation it
        joins the one follow-up instead.
        """
        self._unsub_refresh = None
        if self.hass.is_stopping:
            return
        await self.async_request_evaluation()

    async def async_setup_triggers(self) -> None:
        """Start listening for what should make the watch ask again.

        A window watch hands this to its event log. Otherwise a change to a
        wake_on entity, or to the target when there are none, asks again one
        second later, so a door and a light changing together are one paid
        request. A watch with neither waits for its schedule.
        """
        watch = self.watch_config
        if watch.fire_event:
            self._stops.append(self.async_add_listener(_keep_schedule))
        if watch.window is not None:
            await self._async_setup_window()
            return
        if not watch.wake_on and not watch.target:
            return
        self._debouncer = Debouncer(
            self.hass,
            _LOGGER,
            cooldown=TRIGGER_DEBOUNCE_SECONDS,
            immediate=False,
            function=self.async_schedule_evaluation,
        )
        self._stops.append(self._debouncer.async_shutdown)
        if watch.wake_on:
            stop = async_track_state_change_event(self.hass, watch.wake_on, self._on_change)
        else:
            assert watch.target is not None
            # The target tracker follows the selector, so an entity added to a
            # targeted area later wakes the watch too. It is a coroutine function
            # whatever its annotation says.
            stop = await async_track_target_selector_state_change_event(
                self.hass, dict(watch.target), self._on_change
            )
        self._stops.append(stop)

    @callback
    def _on_change(self, event: Event[EventStateChangedData] | TargetStateChangedData) -> None:
        """A tracked entity changed. Ask again soon, unless the change is no news.

        Mentat's own answers, from this entry or another, say nothing new about
        the house, and waking on them would ask as fast as they answer.
        """
        change = event.state_change_event if isinstance(event, TargetStateChangedData) else event
        if is_mentat_entity(self.hass, change.data["entity_id"]) or self.nobody_reads():
            return
        if self._debouncer is not None:
            self._debouncer.async_schedule_call()

    async def _async_setup_window(self) -> None:
        """Start the event log, which takes over from the plain triggers.

        `interval` fills the log and leaves the asking to the schedule; the other
        two ask once the log settles.
        """
        window = self.watch_config.window
        assert window is not None
        on_change = None if window.evaluate == EVALUATE_INTERVAL else self._window_settled
        self.window = EventWindow(self.hass, self.watch_config, on_change)
        # Before the start, which subscribes and then reads the recorder: a
        # failed read must not leave the subscriptions behind.
        self._stops.append(self.window.async_stop)
        await self.window.async_start()

    @callback
    def _window_settled(self) -> None:
        # An answer nobody reads is still billed.
        if self.nobody_reads():
            return
        self.async_schedule_evaluation()

    @callback
    def async_shutdown_triggers(self) -> None:
        """Stop the window, the trackers and the debouncer. A second call does nothing.

        The debouncer goes too: a call it scheduled just before unload would
        otherwise run against an entry that is gone.
        """
        while self._stops:
            self._stops.pop()()
        self.window = None
        self._debouncer = None

    def _readers(self) -> Iterator[tuple[str, str, bool]]:
        """Each entity that can show this watch's answers.

        Yields the platform, the unique id, and whether the entity counts as a
        reader before it is registered. A question sensor that is not registered
        yet is about to be created and will read the first answer. Verdicts and
        diagnostics exist only for some watches, so they count once registered.
        """
        scope = self.id_scope
        for spec in self.watch_config.questions:
            yield Platform.SENSOR, unique_id(scope, spec.key), True
            yield Platform.BINARY_SENSOR, unique_id(scope, spec.key, UID_VERDICT), False
        for suffix in (UID_LATENCY, UID_BYTES):
            yield Platform.SENSOR, self.diagnostic_id(suffix), False

    def diagnostic_id(self, suffix: str) -> str:
        """The unique id of one of the watch's diagnostics."""
        return unique_id(self.id_scope, UID_WATCH, self.watch_config.key, suffix)

    @callback
    def nobody_reads(self) -> bool:
        """Whether nothing reads this watch's answers, so asking would be waste.

        A watch that fires mentat_decision always has a reader: an automation
        can trigger on the event with every entity disabled. Otherwise the watch
        is read while one of its entities is enabled. The ids are looked up
        whatever entry holds them, since a YAML watch's entities can still belong
        to the entry that ran it before this one.
        """
        if self.watch_config.fire_event:
            return False
        registry = er.async_get(self.hass)
        for platform, uid, unregistered in self._readers():
            entity_id = registry.async_get_entity_id(platform, DOMAIN, uid)
            registered = None if entity_id is None else registry.async_get(entity_id)
            if registered is None:
                if unregistered:
                    return False
            elif registered.disabled_by is None:
                return False
        return True

    async def _async_update_data(self) -> dict[str, Answer]:
        self.runtime.ledger.roll_over(dt_util.now().date())
        now = dt_util.utcnow()
        state = self._build_state(self._render_note(), now)
        questions = {spec.key: spec.question for spec in self.watch_config.questions}
        body_bytes = self.runtime.client.body_size(state, questions)
        response = await self._send(state, questions, body_bytes)
        self._record(response, state, body_bytes, now)
        judged = self._judge(response.answers)
        if self.watch_config.fire_event:
            self._fire_decision(response, judged)
        return response.answers

    def _render_note(self) -> str | None:
        """The watch's note as text. A template that fails stops the evaluation."""
        note = self.watch_config.note
        if note is None:
            return None
        try:
            text: str = note.async_render(parse_result=False)
        except TemplateError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="note_failed",
                translation_placeholders={"watch": self.watch_config.name, "reason": str(err)},
            ) from err
        return text

    def _build_state(self, note: str | None, now: datetime) -> Any:
        """The note alone, the note and the target, or the window's log of the target.

        A picked device or area that has since been removed stops the evaluation.
        Asking about whatever is left would answer a question nobody asked.
        """
        watch = self.watch_config
        try:
            if self.window is None or not watch.target:
                return async_state(
                    self.hass, note, watch.target, with_attributes=watch.with_attributes
                )
            return async_build_window_state(
                self.hass,
                note,
                watch.target,
                self.window,
                now,
                with_attributes=watch.with_attributes,
            )
        except ServiceValidationError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="watch_target_failed",
                translation_placeholders={"watch": watch.name, "reason": str(err)},
            ) from err

    async def _send(
        self, state: Any, questions: dict[str, Question], body_bytes: int
    ) -> JevResponse:
        """Ask, if the estimate fits the day's budget, and turn client errors into HA's.

        A refused key starts reauth. A rate limit waits as long as the server
        asked, which can be longer than the scan interval. Anything else is an
        outage, logged once.
        """
        ledger = self.runtime.ledger
        estimate = ledger.estimate(body_bytes)
        if not ledger.fits(estimate):
            raise self._refuse_over_budget(estimate)
        provider = self.runtime.provider.title
        try:
            with ledger.reserve(estimate):
                return await self.runtime.client.ask(state, questions)
        except JevAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="key_refused",
                translation_placeholders={"provider": provider, "reason": str(err)},
            ) from err
        except JevRateLimitError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="rate_limited",
                translation_placeholders={"provider": provider, "reason": str(err)},
                retry_after=err.retry_after,
            ) from err
        except JevError as err:
            self._track_outage(err)
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="ask_failed",
                translation_placeholders={"provider": provider, "reason": str(err)},
            ) from err

    def _record(self, response: JevResponse, state: Any, body_bytes: int, now: datetime) -> None:
        """Bill the answer, keep what was sent, and tell the ledger's entities."""
        self._track_outage(None)
        runtime = self.runtime
        runtime.ledger.record(response.usage.input_tokens, body_bytes)
        runtime.model_version = response.model or runtime.model_version
        span = None
        if (window := self.window) is not None:
            span = WindowSpan(
                start=now - window.config.duration,
                end=now,
                events=len(window.entries(now)),
                evaluated=dt_util.utcnow(),
            )
        self.last = Exchange(
            state=state, body_bytes=body_bytes, latency_ms=response.latency_ms, window=span
        )
        runtime.ledger.notify()

    def _judge(self, answers: dict[str, Answer]) -> dict[str, Verdict]:
        """Each verdict the new answers give, judged from the ones before them.

        A question the reply left out keeps its last verdict, so a later answer
        between release and cutoff still has something to hold.
        """
        judged: dict[str, Verdict] = {}
        for spec in self.watch_config.questions:
            before = self.verdicts.get(spec.key)
            given = verdict(spec, answers.get(spec.key), None if before is None else before.on)
            if given is not None:
                judged[spec.key] = given
        self.verdicts.update(judged)
        return judged

    @callback
    def _fire_decision(self, response: JevResponse, judged: dict[str, Verdict]) -> None:
        """Put the answers on the bus, so an automation can trigger on a decision.

        A question the reply left out is left out of the event too, and so is a
        verdict nobody can give yet.
        """
        watch = self.watch_config
        answers: dict[str, Any] = {}
        for spec in watch.questions:
            if (answer := response.answers.get(spec.key)) is None:
                continue
            answers[spec.key] = shown = answer_as_dict(answer) | {"name": spec.name}
            if (sure := confident(spec.min_confidence, answer)) is not None:
                shown[ATTR_CONFIDENT] = sure
            if (decided := judged.get(spec.key)) is None:
                continue
            if decided.on is not None:
                shown["verdict"] = decided.on
            if spec.release is not None:
                shown[ATTR_HELD] = decided.held
        self.hass.bus.async_fire(
            EVENT_DECISION,
            {
                "entry_id": self.config_entry.entry_id,
                "watch": watch.key,
                "watch_name": watch.name,
                "model": response.model,
                "latency_ms": round(response.latency_ms, 1),
                "input_tokens": response.usage.input_tokens,
                "window": self._window_summary(),
                "answers": answers,
            },
        )

    def _window_summary(self) -> dict[str, Any] | None:
        """When the last answered window opened and closed, and what it held."""
        span = self.window_span
        if span is None:
            return None
        return {
            "start": span.start.isoformat(),
            "end": span.end.isoformat(),
            "events": span.events,
        }

    def _track_outage(self, err: Exception | None) -> None:
        """Log the first failure of an outage and the first answer after it, once each.

        `err` is None when the server answered.
        """
        down = err is not None
        if down == self._server_down:
            return
        self._server_down = down
        provider, watch = self.runtime.provider.title, self.watch_config.name
        if err is None:
            _LOGGER.info("%s is answering again; watch %r resumed", provider, watch)
        else:
            _LOGGER.error(
                "%s is not answering, so watch %r cannot be evaluated: %s", provider, watch, err
            )

    def _refuse_over_budget(self, estimate: int) -> UpdateFailed:
        """Flag the ledger, log the first refusal of the day, and return the error to raise.

        The flag raises the repair issue. The sensors go unavailable, which is the
        honest reading: nothing was asked, so there is no answer for now.
        """
        ledger = self.runtime.ledger
        first = not ledger.over_budget
        ledger.mark_over_budget(True, used=ledger.tokens)
        ledger.notify()
        name = self.watch_config.name
        if first:
            _LOGGER.error(
                "Stopped evaluating: the daily budget is %s input tokens, %s are used "
                "and watch %r needs about %s more. Raise the budget in the "
                "integration options or ask less often",
                ledger.budget,
                ledger.tokens,
                name,
                estimate,
            )
        return UpdateFailed(
            translation_domain=DOMAIN,
            translation_key="watch_over_budget",
            translation_placeholders={
                "watch": name,
                "estimate": str(estimate),
                "remaining": str(ledger.remaining()),
                "budget": str(ledger.budget),
            },
        )
