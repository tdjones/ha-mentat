"""A cutoff and a release for one noul, tried against what really happened.

Numbers picked by eye are guesses about one house. This replays a noul's recorded
probabilities through the rule its verdict sensor uses, judge.side(), including
the hold between release and cutoff, and compares the verdict that comes out with
an entity that shows the truth. A verdict is read the way an automation meets it,
as stretches of on and off, so the replay counts true stretches caught and missed,
alarms with nothing behind them, and flips.

It also says whether any numbers can work. When the probabilities of true time
and false time overlap, no cutoff separates them and the question needs rewording.

Nothing here sends a request, so it costs no tokens.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from itertools import pairwise
from typing import Any, Final

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.recorder import get_instance

from .judge import side

# The numbers tried, in twentieths. A choice nobody has measured: a person tunes no
# finer, and each pair costs a pass over the whole history.
_GRID: Final = tuple(step / 20 for step in range(1, 20))

# How far a pair's F1 may fall short of the best and still compete on calm, as a
# share of the best. Relative, so a verdict that is never on cannot win on calm
# when the best F1 is low. A choice nobody has measured.
_CALM_MARGIN: Final = 0.9

# How much of a side's time its band leaves out at each end, so one stray answer
# does not stretch the band over the other side. A choice nobody has measured.
_BAND_TAIL: Final = 0.1


@dataclass(frozen=True, slots=True)
class Piece:
    """A stretch of time in which neither entity changed."""

    probability: float
    truth: bool
    seconds: float


def number(state: str) -> float | None:
    """A recorded state as a number, or None for unavailable and the like."""
    try:
        return float(state)
    except ValueError:
        return None


def _value_at(times: list[datetime], values: list[str], when: datetime) -> str | None:
    """The value in force at this moment, or None before the first one."""
    index = bisect_right(times, when)
    return values[index - 1] if index else None


def _true_spells(
    real: list[tuple[datetime, str]], truth_state: str, end: datetime, grace: timedelta
) -> list[tuple[datetime, datetime]]:
    """When the truth held, each spell stretched by the grace and joined to the next."""
    spells: list[tuple[datetime, datetime]] = []
    finishes = [when for when, _ in real[1:]] + [end]
    for (begin, value), finish in zip(real, finishes, strict=True):
        if value != truth_state:
            continue
        until = min(finish + grace, end)
        if spells and begin <= spells[-1][1]:
            spells[-1] = (spells[-1][0], max(spells[-1][1], until))
        else:
            spells.append((begin, until))
    return spells


def pieces_of(
    probabilities: Sequence[State],
    truths: Sequence[State],
    truth_state: str,
    start: datetime,
    end: datetime,
    grace: timedelta,
) -> list[Piece]:
    """The time from start to end, cut wherever either entity changed.

    A state from before the start holds from the start. Time when the sensor held
    no number, such as unavailable during an outage, is left out, because no
    verdict was given then.

    An answer comes after what it describes, by up to the watch's schedule, so the
    truth counts as still holding for `grace` after it ended. Without that, a
    10 second event answered a minute later scores a miss and a false alarm.
    """
    said = sorted((max(state.last_changed, start), state.state) for state in probabilities)
    real = sorted((max(state.last_changed, start), state.state) for state in truths)
    said_times, said_values = [when for when, _ in said], [value for _, value in said]
    real_times, real_values = [when for when, _ in real], [value for _, value in real]
    spells = _true_spells(real, truth_state, end, grace)
    spell_starts = [begin for begin, _ in spells]
    pieces: list[Piece] = []
    cuts = {*said_times, *real_times, *(finish for _, finish in spells), end}
    for begin, finish in pairwise(sorted(cuts)):
        reading = _value_at(said_times, said_values, begin)
        probability = None if reading is None else number(reading)
        if probability is None or _value_at(real_times, real_values, begin) is None:
            continue
        spell = bisect_right(spell_starts, begin) - 1
        truth = spell >= 0 and begin < spells[spell][1]
        pieces.append(Piece(probability, truth, (finish - begin).total_seconds()))
    return pieces


@dataclass(frozen=True, slots=True)
class Replay:
    """What a verdict with these numbers would have done."""

    cutoff: float
    release: float
    on_while_true: float
    on_while_false: float
    off_while_true: float
    caught: int
    missed: int
    false_alarms: int
    flips: int

    @property
    def precision(self) -> float | None:
        """How much of the time on was true. None for a verdict that was never on."""
        on = self.on_while_true + self.on_while_false
        return self.on_while_true / on if on else None

    @property
    def recall(self) -> float | None:
        """How much of the true time was on. None when there was no true time."""
        true = self.on_while_true + self.off_while_true
        return self.on_while_true / true if true else None

    @property
    def f1(self) -> float:
        precision, recall = self.precision, self.recall
        if not precision or not recall:
            return 0.0
        return 2 * precision * recall / (precision + recall)

    def as_dict(self) -> dict[str, Any]:
        precision, recall = self.precision, self.recall
        return {
            "precision": None if precision is None else round(precision, 3),
            "recall": None if recall is None else round(recall, 3),
            "f1": round(self.f1, 3),
            "caught": self.caught,
            "missed": self.missed,
            "false_alarms": self.false_alarms,
            "flips": self.flips,
        }


def replay(pieces: Sequence[Piece], cutoff: float, release: float) -> Replay:
    """Walk the verdict through the pieces, as binary_sensor would have shown it.

    The verdict starts unknown, as after a restart. An unknown verdict triggers
    nothing, so true time under one counts as missed, or a pair that never decides
    would score perfectly. The first decision is no flip.
    """
    verdict: bool | None = None
    on_while_true = on_while_false = off_while_true = 0.0
    caught = missed = false_alarms = flips = 0
    alarm_was_real = False
    in_truth = truth_caught = False
    for piece in pieces:
        decided = side(piece.probability, cutoff, release)
        if decided is not None and decided != verdict:
            if verdict is not None:
                flips += 1
            if verdict and not alarm_was_real:
                false_alarms += 1
            verdict, alarm_was_real = decided, False
        if in_truth and not piece.truth and not truth_caught:
            missed += 1
        if piece.truth and not in_truth:
            truth_caught = False
        in_truth = piece.truth
        if verdict and piece.truth:
            alarm_was_real = True
            if not truth_caught:
                caught += 1
                truth_caught = True
        if verdict:
            if piece.truth:
                on_while_true += piece.seconds
            else:
                on_while_false += piece.seconds
        elif piece.truth:
            off_while_true += piece.seconds
    if verdict and not alarm_was_real:
        false_alarms += 1
    if in_truth and not truth_caught:
        missed += 1
    return Replay(
        cutoff,
        release,
        on_while_true,
        on_while_false,
        off_while_true,
        caught,
        missed,
        false_alarms,
        flips,
    )


def best_replay(pieces: Sequence[Piece]) -> Replay:
    """The calmest pair whose F1 is close to the best.

    A verdict that flips all day is what a release is for, so a pair that scores a
    little lower and flips far less is the better one. Many pairs usually tie,
    because no probability fell between them. The middle of the ties is taken, not
    an edge, so an answer a little off its usual value still lands on the same side.
    """
    tried = [
        replay(pieces, cutoff, release)
        for cutoff in _GRID
        for release in _GRID
        if release <= cutoff
    ]
    enough = max(each.f1 for each in tried) * _CALM_MARGIN
    close = [each for each in tried if each.f1 >= enough]
    calmest = min(each.flips for each in close)
    tied = [each for each in close if each.flips == calmest]
    cutoff = _middle([each.cutoff for each in tied])
    release = _middle([each.release for each in tied if each.cutoff == cutoff])
    return replay(pieces, cutoff, release)


def _middle(values: list[float]) -> float:
    """The middle value, the lower of two, so the answer is a number that was tried."""
    return sorted(values)[(len(values) - 1) // 2]


def band(pieces: Sequence[Piece]) -> dict[str, float]:
    """The probabilities that held for most of this time, weighted by how long."""
    ordered = sorted(pieces, key=lambda piece: piece.probability)
    total = sum(piece.seconds for piece in ordered)
    low = high = ordered[0].probability
    passed = 0.0
    for piece in ordered:
        if passed <= total * _BAND_TAIL:
            low = piece.probability
        passed += piece.seconds
        if passed - piece.seconds < total * (1 - _BAND_TAIL):
            high = piece.probability
    return {"low": low, "high": high}


def calibration(pieces: Sequence[Piece], answers: int) -> dict[str, Any]:
    """The action's response, for pieces holding both true and false time."""
    chosen = best_replay(pieces)
    alone = replay(pieces, chosen.cutoff, chosen.cutoff)
    when_true = band([piece for piece in pieces if piece.truth])
    when_false = band([piece for piece in pieces if not piece.truth])
    return {
        "cutoff": chosen.cutoff,
        "release": chosen.release,
        **chosen.as_dict(),
        "without_release": alone.as_dict(),
        "when_true": when_true,
        "when_false": when_false,
        "overlap": when_true["low"] <= when_false["high"],
        "hours": round(sum(piece.seconds for piece in pieces) / 3600, 2),
        "hours_true": round(sum(piece.seconds for piece in pieces if piece.truth) / 3600, 2),
        "answers": answers,
    }


class RecorderUnreadableError(Exception):
    """The recorder's database could not be read."""


async def async_recorded_states(
    hass: HomeAssistant, start: datetime, end: datetime, entity_ids: list[str]
) -> dict[str, list[State]]:
    """Every recorded state change of these entities from start to end.

    Raises RecorderUnreadableError when the database cannot be read.
    """
    # Imported here: the recorder is an after-dependency, not a dependency, and
    # these modules load SQLAlchemy.
    from homeassistant.components.recorder.history import get_significant_states
    from sqlalchemy.exc import SQLAlchemyError

    # Significant changes only: rows where the state itself changed. A question
    # sensor's window attributes change on every answer and say nothing here.
    query = partial(
        get_significant_states,
        hass,
        start,
        end,
        entity_ids,
        include_start_time_state=True,
        significant_changes_only=True,
        no_attributes=True,
    )
    try:
        found = await get_instance(hass).async_add_executor_job(query)
    except SQLAlchemyError as err:
        raise RecorderUnreadableError(str(err)) from err
    return {
        entity_id: [state for state in found.get(entity_id, []) if isinstance(state, State)]
        for entity_id in entity_ids
    }
