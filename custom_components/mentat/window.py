"""A window of events: what changed over the last stretch of time.

A watch with a window is judged on a log as well as on the current readings.
The log holds state changes of the watch's entities and the bus events it names,
oldest first, at most `max_events` of them. On start it is filled from the
recorder, so a restart does not leave the model looking at an empty hour. Bus
events are not in the recorder's state history, so they start empty after a
restart and fill as they happen.

`async_build_window_state` turns the readings and the log into the state a window
watch sends. readings.py knows nothing of windows, so the import runs one way.

A window entry without attributes is about 200 bytes, so 500 of them, the cap,
are about 100 kB per watch.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import Any, Literal

from homeassistant.const import ATTR_ENTITY_ID, ATTR_FRIENDLY_NAME, EVENT_STATE_CHANGED, STATE_ON
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
    split_entity_id,
)
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.recorder import get_instance
from homeassistant.helpers.target import (
    TargetSelection,
    TargetStateChangedData,
    async_extract_referenced_entity_ids,
    async_track_target_selector_state_change_event,
)
from homeassistant.util import dt as dt_util

from .const import MAX_WINDOW_ENTITIES, WINDOW_SETTLE_CAP_FACTOR
from .readings import (
    ago,
    async_records,
    attribute_subset,
    is_mentat_entity,
    json_safe,
    redact,
    span,
)
from .watches import EventFilter, WatchConfig, WindowConfig, is_own_event

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class WindowEntry:
    """One line of the log: a state change, or a bus event that matched a filter."""

    at: datetime
    kind: Literal["state", "event"]
    entity_id: str | None
    name: str
    old: str | None
    new: str | None
    attributes: dict[str, Any] | None = None
    # The bus event's type, for an event entry.
    event_type: str | None = None


def matches(event_filter: EventFilter, data: Mapping[str, Any]) -> bool:
    """Whether every key the filter names is in the event data with that value."""
    return all(key in data and data[key] == value for key, value in event_filter.data.items())


def _cap(entity_ids: set[str]) -> set[str]:
    """The first MAX_WINDOW_ENTITIES ids in order, so the cut does not move."""
    return set(sorted(entity_ids)[:MAX_WINDOW_ENTITIES])


class EventWindow:
    """The log one window watch reads, kept in memory while the entry is loaded.

    `on_change` runs once the log has been quiet for `settle` seconds: every
    entry restarts the wait, so a door, a light and a motion sensor firing
    together are judged once. A sensor that never goes quiet cannot put the run
    off forever, because the wait ends WINDOW_SETTLE_CAP_FACTOR settle periods
    after the first entry still waiting. A settle of 0 runs it on every entry.
    Without `on_change` the log only fills.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        watch: WatchConfig,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        if watch.window is None:
            raise ValueError(f"watch {watch.name!r} has no window")
        self.hass = hass
        self.watch = watch
        self.config: WindowConfig = watch.window
        self.entity_ids: list[str] = []
        self._buffer: deque[WindowEntry] = deque(maxlen=self.config.max_events)
        # The newest entry pushed out of a full buffer. If it is inside the
        # window, the log the model reads is missing its start.
        self._dropped: datetime | None = None
        self._last_new: dict[str, str | None] = {}
        self._unsubs: list[CALLBACK_TYPE] = []
        # Live entries that arrive while the recorder is still being read; they
        # are logged after the backfill so the log stays in order.
        self._pending: list[WindowEntry] | None = None
        self._on_change = on_change
        self._unsub_settle: CALLBACK_TYPE | None = None
        # Loop time of the first entry waiting for the run, for the cap.
        self._waiting_since: float | None = None

    @property
    def buffered(self) -> int:
        """Entries held, inside the window or not."""
        return len(self._buffer)

    def entries(self, now: datetime) -> list[WindowEntry]:
        """The entries within [now - duration, now], oldest first."""
        start = now - self.config.duration
        return [entry for entry in self._buffer if start <= entry.at <= now]

    def truncated(self, now: datetime) -> bool:
        """Whether entries inside the window were dropped to keep max_events."""
        return self._dropped is not None and self._dropped >= now - self.config.duration

    async def async_start(self, *, live: bool = True) -> None:
        """Resolve the entities, read the recorder, then follow changes if `live`.

        The subscription comes first, so a change made while the recorder is read
        is held and logged after the backfill rather than lost between the two.
        A held change starts no settle wait: the watch asks once right after
        start and reads it then, and a wait would bill the same state again.
        Whatever the backfill raises, the held changes are logged and later
        ones go straight in.
        """
        self.entity_ids = self._resolve()
        now = dt_util.utcnow()
        if live:
            self._pending = []
            await self._async_subscribe()
        try:
            for entry in await self._async_backfill(now):
                self._accept(entry, settle=False)
        finally:
            pending, self._pending = self._pending or [], None
            for entry in pending:
                self._accept(entry, settle=False)

    @callback
    def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        # A run scheduled just before unload would otherwise fire into a
        # coordinator with no entry behind it.
        self._on_change = None
        self._cancel_settle()

    @callback
    def _resolve(self) -> list[str]:
        """The entities whose changes are logged, in id order, at most the cap."""
        watch = self.watch
        if watch.wake_on:
            wanted = set(watch.wake_on)
        elif watch.target:
            selected = async_extract_referenced_entity_ids(
                self.hass, TargetSelection(dict(watch.target)), expand_group=True
            )
            wanted = selected.referenced | selected.indirectly_referenced
        else:
            wanted = set()
        if len(wanted) > MAX_WINDOW_ENTITIES:
            _LOGGER.warning(
                "Watch %r targets %s entities; its window logs the first %s. The "
                "evaluation itself is refused until the target is smaller",
                watch.name,
                len(wanted),
                MAX_WINDOW_ENTITIES,
            )
        return sorted(_cap(wanted))

    async def _async_subscribe(self) -> None:
        watch = self.watch
        if watch.wake_on:
            self._unsubs.append(
                async_track_state_change_event(self.hass, self.entity_ids, self._state_changed)
            )
        elif watch.target:
            # The target rather than the resolved list, so an entity added to a
            # targeted area later is logged too. The tracker is a coroutine
            # function despite its return annotation, so it is awaited.
            self._unsubs.append(
                await async_track_target_selector_state_change_event(
                    self.hass, dict(watch.target), self._target_changed, _cap
                )
            )
        for event_filter in self.config.events:
            self._unsubs.append(self._listen(event_filter))

    @callback
    def _listen(self, event_filter: EventFilter) -> CALLBACK_TYPE:
        @callback
        def _heard(event: Event[dict[str, Any]]) -> None:
            # A filter saved before `*` and state_changed were refused can still
            # hear Mentat's answers. Logging one would ask again after each.
            if is_own_event(event.event_type) or self._is_own_change(event):
                return
            if not matches(event_filter, event.data):
                return
            # Plain data first, so the redaction sees inside what the bus
            # carries as objects: a state_changed event holds State objects
            # whose attributes can hold an entity_picture or a token.
            data = redact(json_safe(event.data))
            self._offer(
                WindowEntry(
                    at=event.time_fired,
                    kind="event",
                    entity_id=None,
                    name=event_filter.label or event_filter.event_type,
                    old=None,
                    new=None,
                    attributes=data or None,
                    event_type=event_filter.event_type,
                )
            )

        return self.hass.bus.async_listen(event_filter.event_type, _heard)

    def _is_own_change(self, event: Event[dict[str, Any]]) -> bool:
        return event.event_type == EVENT_STATE_CHANGED and is_mentat_entity(
            self.hass, str(event.data.get(ATTR_ENTITY_ID))
        )

    @callback
    def _target_changed(self, data: TargetStateChangedData) -> None:
        self._state_changed(data.state_change_event)

    @callback
    def _state_changed(self, event: Event[EventStateChangedData]) -> None:
        new = event.data["new_state"]
        # A removed entity has nothing to say about what the house is doing,
        # and Mentat's own answers are not news about it either: logging them
        # would settle the window after each of its own answers.
        if new is None or is_mentat_entity(self.hass, new.entity_id):
            return
        old = event.data["old_state"]
        self._offer(
            WindowEntry(
                # last_updated, because an attribute-only update keeps its old
                # last_changed and would land in the past.
                at=new.last_updated,
                kind="state",
                entity_id=new.entity_id,
                name=str(new.attributes.get(ATTR_FRIENDLY_NAME) or new.entity_id),
                old=old.state if old is not None else None,
                new=new.state,
                attributes=self._attributes(new),
            )
        )

    def _attributes(self, state: State) -> dict[str, Any] | None:
        if not self.config.with_attributes:
            return None
        return attribute_subset(json_safe(state.attributes)) or None

    @callback
    def _offer(self, entry: WindowEntry) -> None:
        if self._pending is not None:
            self._pending.append(entry)
            return
        self._accept(entry)

    @callback
    def _accept(self, entry: WindowEntry, *, settle: bool = True) -> None:
        if entry.kind == "state":
            key = entry.entity_id or ""
            if self.config.collapse_repeats and entry.new == self._last_new.get(key, entry.old):
                return
            self._last_new[key] = entry.new
        if len(self._buffer) == self._buffer.maxlen:
            self._dropped = self._buffer[0].at
        self._buffer.append(entry)
        if settle:
            self._schedule_settle()

    @callback
    def _schedule_settle(self) -> None:
        """Run on_change after `settle` seconds of quiet, capped from the first entry."""
        if self._on_change is None:
            return
        settle = self.config.settle
        if settle <= 0:
            self._on_change()
            return
        now = self.hass.loop.time()
        if self._waiting_since is None:
            self._waiting_since = now
        deadline = self._waiting_since + WINDOW_SETTLE_CAP_FACTOR * settle
        if self._unsub_settle is not None:
            self._unsub_settle()
        self._unsub_settle = async_call_later(
            self.hass, max(0.0, min(settle, deadline - now)), self._settled
        )

    @callback
    def _settled(self, _now: datetime) -> None:
        self._unsub_settle = None
        self._waiting_since = None
        if self._on_change is not None:
            self._on_change()

    @callback
    def _cancel_settle(self) -> None:
        if self._unsub_settle is not None:
            self._unsub_settle()
            self._unsub_settle = None
        self._waiting_since = None

    async def _async_backfill(self, now: datetime) -> list[WindowEntry]:
        """The window's changes as the recorder has them, newest max_events kept.

        One query reads every entity. The recorder's own `limit` keeps the oldest
        rows of a period, which would drop the changes that matter most, so each
        entity is read whole and the cut is made here.
        """
        if "recorder" not in self.hass.config.components:
            return []
        wanted = [
            entity_id for entity_id in self.entity_ids if not is_mentat_entity(self.hass, entity_id)
        ]
        if not wanted:
            return []
        # Imported here: the recorder is an after-dependency, not a dependency,
        # and these modules load SQLAlchemy.
        from homeassistant.components.recorder.history import (
            SIGNIFICANT_DOMAINS,
            get_significant_states,
        )
        from sqlalchemy.exc import SQLAlchemyError

        start = now - self.config.duration
        # Significant changes only: rows where the state itself changed, which is
        # what the log is made of. In SIGNIFICANT_DOMAINS, climate among them,
        # the recorder counts attribute updates as well, and those are dropped.
        query = partial(
            get_significant_states,
            self.hass,
            start,
            now,
            wanted,
            include_start_time_state=True,
            significant_changes_only=True,
            no_attributes=not self.config.with_attributes,
        )
        try:
            history = await get_instance(self.hass).async_add_executor_job(query)
        except SQLAlchemyError as err:
            _LOGGER.warning(
                "Watch %r: the recorder could not be read, so its window starts empty: %s",
                self.watch.name,
                err,
            )
            return []
        entries: list[WindowEntry] = []
        for entity_id in wanted:
            states = [state for state in history.get(entity_id, []) if isinstance(state, State)]
            same_state_rows = split_entity_id(entity_id)[0] in SIGNIFICANT_DOMAINS
            entries.extend(self._from_history(entity_id, states, start, same_state_rows))
        entries.sort(key=lambda entry: entry.at)
        max_events = self.config.max_events
        if len(entries) > max_events:
            self._dropped = entries[-max_events - 1].at
            entries = entries[-max_events:]
        return entries

    def _from_history(
        self, entity_id: str, states: list[State], start: datetime, same_state_rows: bool
    ) -> list[WindowEntry]:
        """Entries from one entity's consecutive states.

        The first state can be the one in force when the window opened; it is the
        `from` of the first change rather than a change of its own. With
        `same_state_rows` a row can be an attribute update that kept the state,
        and that is no change.
        """
        current = self.hass.states.get(entity_id)
        name = entity_id
        if current is not None:
            name = str(current.attributes.get(ATTR_FRIENDLY_NAME) or entity_id)
        entries: list[WindowEntry] = []
        previous: str | None = None
        for state in states:
            if state.last_changed <= start:
                previous = state.state
                continue
            if same_state_rows and state.state == previous:
                continue
            entries.append(
                WindowEntry(
                    at=state.last_changed,
                    kind="state",
                    entity_id=entity_id,
                    name=name,
                    old=previous,
                    new=state.state,
                    attributes=self._attributes(state),
                )
            )
            previous = state.state
        return entries


async def async_window_state_once(
    hass: HomeAssistant,
    note: Any,
    target: Mapping[str, Any],
    with_attributes: bool,
    config: WindowConfig,
    wake_on: Sequence[str] = (),
) -> dict[str, Any]:
    """A window read from the recorder once, for an action or the editor's preview.

    Nothing is subscribed: the window is built, read and dropped in one go.
    """
    watch = WatchConfig(
        key="once",
        name="once",
        questions=(),
        scan_interval=0,
        target=target,
        with_attributes=with_attributes,
        wake_on=tuple(wake_on),
        window=config,
    )
    window = EventWindow(hass, watch)
    await window.async_start(live=False)
    try:
        return async_build_window_state(
            hass, note, target, window, dt_util.utcnow(), with_attributes=with_attributes
        )
    finally:
        window.async_stop()


def _clock(moment: datetime, now: datetime) -> str:
    """Local HH:MM, with the weekday when it is not today's."""
    local, today = dt_util.as_local(moment), dt_util.as_local(now)
    if local.date() != today.date():
        return local.strftime("%a %H:%M")
    return local.strftime("%H:%M")


def _on_time(
    current: State, changes: list[WindowEntry], start: datetime, now: datetime
) -> tuple[timedelta, datetime | None]:
    """How long a binary sensor was on inside the window, and when it last was.

    Before its first logged change it held that change's `from`. With no change
    logged it held its current state, though only since it last changed: what it
    did before that is not known, so it is not counted.
    """
    total = timedelta(0)
    last_on: datetime | None = None
    if changes:
        state, since = changes[0].old, start
    else:
        state, since = current.state, max(start, current.last_changed)
    for change in changes:
        if state == STATE_ON:
            total += change.at - since
            last_on = change.at
        state, since = change.new, change.at
    if state == STATE_ON:
        total += now - since
        last_on = now
    return total, last_on


def _event_row(entry: WindowEntry, now: datetime) -> dict[str, Any]:
    row: dict[str, Any] = {"at": _clock(entry.at, now), "ago": ago(now - entry.at)}
    if entry.kind == "event":
        row["event"] = entry.event_type
        if entry.name != entry.event_type:
            row["name"] = entry.name
        if entry.attributes:
            row["data"] = entry.attributes
        return row
    row["entity_id"] = entry.entity_id
    if entry.name != entry.entity_id:
        row["name"] = entry.name
    row["from"] = entry.old
    row["to"] = entry.new
    if entry.attributes:
        row["attributes"] = entry.attributes
    return row


@callback
def async_build_window_state(
    hass: HomeAssistant,
    note: Any | None,
    target: Mapping[str, Any],
    window: EventWindow,
    now: datetime,
    *,
    with_attributes: bool = False,
) -> dict[str, Any]:
    """The state of a window watch: the readings now and the log behind them.

    Each entity carries what the log says about it, worked out here: how long
    it has held its state, how often it changed in the window, and for a binary
    sensor how long it was on and when it last was. A model that has to subtract
    timestamps answers worse than one that reads the result.
    """
    config = window.config
    start = now - config.duration
    entries = window.entries(now)
    changes: dict[str, list[WindowEntry]] = defaultdict(list)
    for entry in entries:
        if entry.kind == "state" and entry.entity_id is not None:
            changes[entry.entity_id].append(entry)
    records = async_records(
        hass, target, with_attributes=with_attributes, limit=MAX_WINDOW_ENTITIES
    )
    for record in records:
        entity_id = record["entity_id"]
        # Present: async_records describes only entities with a state.
        state = hass.states.get(entity_id)
        assert state is not None
        record.pop("changed", None)
        record["since"] = ago(now - state.last_changed)
        mine = changes.get(entity_id, [])
        record["changes_in_window"] = len(mine)
        if state.domain == "binary_sensor":
            on_total, last_on = _on_time(state, mine, start, now)
            if last_on is not None:
                record["last_on"] = "now" if last_on == now else ago(now - last_on)
            record["on_total"] = span(on_total)
    payload: dict[str, Any] = {
        "now": dt_util.as_local(now).strftime("%Y-%m-%d %H:%M %A"),
        "window": {
            "from": _clock(start, now),
            "to": _clock(now, now),
            "minutes": round(config.duration.total_seconds() / 60),
            "events": len(entries),
            "truncated": window.truncated(now),
        },
        "entities": records,
        "events": [_event_row(entry, now) for entry in entries],
    }
    if note:
        payload["note"] = note
    return payload
