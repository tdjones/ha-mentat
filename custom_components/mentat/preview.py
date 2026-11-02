"""What a question built in the UI will send, and what it answers, before it is saved.

The editor's last step shows three things as markdown: the state the question
reads, built now from what it points at; which saved questions share its
request; and one real answer to it. The answer is a paid request, so it goes
through services.async_ask and the day's budget can refuse it.

Nothing here raises. A template with a typo, a target that is gone or a server
that is down is what this screen exists to show, so each becomes a sentence
from the `common` or `exceptions` strings and the question can still be saved.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from functools import partial
from typing import Any, Final

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, TemplateError
from homeassistant.helpers import translation
from jevclient import Answer, ChoiceAnswer, NoulAnswer, ScoreAnswer

from .const import DOMAIN, SUBENTRY_QUESTION
from .readings import async_state
from .services import async_ask
from .watches import (
    WatchConfig,
    cutoff_from,
    question_from_mapping,
    share_key,
    watch_from_question,
)
from .window import async_window_state_once

# The screen is for spotting a state that says something other than its author
# meant. Past this many characters nobody reads on, so the rest is counted.
PREVIEW_CHARS: Final = 1800
BAR_WIDTH: Final = 18
# The key the trial question is asked under. The server answers under it.
TRIAL_KEY: Final = "preview"

_FULL: Final = "█"
_EMPTY: Final = "░"

type Say = Callable[..., str]


async def _catalogue(hass: HomeAssistant, category: str) -> dict[str, str]:
    return await translation.async_get_translations(hass, hass.config.language, category, [DOMAIN])


def _say(catalogue: Mapping[str, str], key: str, /, **placeholders: str) -> str:
    # A missing sentence shows its key, which a test or a user can report.
    return catalogue.get(f"component.{DOMAIN}.common.{key}", key).format(**placeholders)


async def sentences(hass: HomeAssistant) -> Say:
    """A function that turns a `common` key and its placeholders into text."""
    return partial(_say, await _catalogue(hass, "common"))


async def readable(hass: HomeAssistant, err: HomeAssistantError) -> str:
    """The sentence an exception would show in the UI.

    The catalogue is read here rather than taken from str(err), which is empty
    for a translated error until Home Assistant has cached its strings.
    """
    catalogue = await _catalogue(hass, "exceptions")
    found = catalogue.get(f"component.{DOMAIN}.exceptions.{err.translation_key}.message")
    if found is None:
        return str(err)
    return found.format(**(err.translation_placeholders or {}))


def _clip(state: Any) -> str:
    text = state if isinstance(state, str) else json.dumps(state, indent=2, default=str)
    left_out = len(text) - PREVIEW_CHARS
    if left_out <= 0:
        return text
    return f"{text[:PREVIEW_CHARS]}\n... {left_out} more characters"


async def _read(hass: HomeAssistant, draft: WatchConfig, note: str | None) -> Any:
    if draft.window is None or not draft.target:
        return async_state(hass, note, draft.target, with_attributes=draft.with_attributes)
    # Read from the recorder the way the saved question's first request will
    # be. The window is dropped as soon as the state is built.
    return await async_window_state_once(
        hass, note, draft.target, draft.with_attributes, draft.window, draft.wake_on
    )


async def draft_state(hass: HomeAssistant, data: Mapping[str, Any]) -> tuple[str, Any]:
    """The state as the screen shows it, and the state itself.

    After a failure the first item is the sentence saying why and the state is
    None, so the caller knows there is nothing to ask about.
    """
    draft = watch_from_question(hass, data)
    note: str | None = None
    if draft.note is not None:
        try:
            note = draft.note.async_render(parse_result=False)
        except TemplateError as err:
            say = await sentences(hass)
            return say("trial_template_error", reason=str(err)), None
    try:
        state = await _read(hass, draft, note)
    except HomeAssistantError as err:
        return await readable(hass, err), None
    return _clip(state), state


@dataclass(frozen=True, slots=True)
class _Bar:
    """A probability drawn as a fixed-width bar, with what it is the probability of."""

    label: str
    p: float
    bold: bool = False

    @property
    def cells(self) -> str:
        filled = min(BAR_WIDTH, max(0, round(self.p * BAR_WIDTH)))
        return _FULL * filled + _EMPTY * (BAR_WIDTH - filled)

    def row(self) -> str:
        label = f"**{self.label}**" if self.bold else self.label
        return f"`{self.cells}` {self.p:.2f}  {label}"


def _draw_noul(answer: NoulAnswer, cutoff: float | None, say: Say) -> list[str]:
    drawn = [f"`{_Bar('', answer.noul).cells}`  **{answer.noul:.2f}**"]
    if cutoff is not None:
        key = "trial_cutoff_over" if answer.noul >= cutoff else "trial_cutoff_under"
        drawn.append(say(key, cutoff=f"{cutoff:g}"))
    return drawn


def _draw_choice(answer: ChoiceAnswer, cutoff: float | None, say: Say) -> list[str]:
    # Likeliest first, so the eye lands on the winner.
    ranked = sorted(answer.probabilities.items(), key=lambda item: item[1], reverse=True)
    bars = [_Bar(name, p, bold=name == answer.choice) for name, p in ranked]
    verdict = say("trial_winner", choice=answer.choice, confidence=f"{answer.confidence:.2f}")
    return ["\n".join(bar.row() for bar in bars), verdict]


def _draw_score(answer: ScoreAnswer, cutoff: float | None, say: Say) -> list[str]:
    # Lowest level first, the order the author wrote them in.
    bars = [
        _Bar(answer.legend.get(level, level), p)
        for level, p in sorted(answer.probabilities.items())
    ]
    verdict = say(
        "trial_score",
        score=f"{answer.score:.2f}",
        level=answer.nearest_level,
        confidence=f"{answer.confidence:.2f}",
    )
    return ["\n".join(bar.row() for bar in bars), verdict]


_DRAWERS: Final[dict[type[Answer], Callable[[Any, float | None, Say], list[str]]]] = {
    NoulAnswer: _draw_noul,
    ChoiceAnswer: _draw_choice,
    ScoreAnswer: _draw_score,
}


def draw_answer(answer: Answer, cutoff: float | None, say: Say) -> str:
    """The answer as bars. Bars rank at a glance, where a column of decimals does not."""
    drawer = _DRAWERS.get(type(answer))
    if drawer is None:
        return say("trial_undrawable")
    return "\n\n".join(drawer(answer, cutoff, say))


async def trial(
    hass: HomeAssistant, entry: ConfigEntry, data: Mapping[str, Any], state: Any
) -> str:
    """Ask the draft question once, now, and draw what comes back.

    A state that reads well to a person can still get 0.5 back, and that is the
    surprise worth catching before the question is saved. The trial costs a few
    hundred tokens and counts against the day's budget like any request.
    """
    say = await sentences(hass)
    if state is None:
        return say("trial_nothing_yet")
    question = question_from_mapping(data)
    try:
        response = await async_ask(hass, entry, state, {TRIAL_KEY: question})
    except HomeAssistantError as err:
        if err.translation_key == "request_over_budget":
            return say("trial_over_budget")
        return say("trial_failed", reason=await readable(hass, err))
    answer = response.answers.get(TRIAL_KEY)
    if answer is None:
        return say("trial_mismatched")
    billed = response.usage.input_tokens
    usd = billed * entry.runtime_data.ledger.token_price / 1_000_000
    spent = say(
        "trial_cost", tokens=str(billed), cost=f"{usd:.6f}", ms=f"{response.latency_ms:.0f}"
    )
    return draw_answer(answer, cutoff_from(data), say) + f"\n\n_{spent}_"


async def describe_window(hass: HomeAssistant, state: Any) -> str | None:
    """How much the window holds so far, or None when the question has none."""
    summary = state.get("window") if isinstance(state, dict) else None
    if not isinstance(summary, dict):
        return None
    say = await sentences(hass)
    minutes = str(summary["minutes"])
    if "recorder" in hass.config.components:
        return say("trial_window", minutes=minutes, events=str(summary["events"]))
    return say("trial_window_no_recorder", minutes=minutes)


async def describe_sharing(
    hass: HomeAssistant, entry: ConfigEntry, data: Mapping[str, Any], editing: str | None
) -> str:
    """Which saved questions will go in one request with this one.

    `editing` is the subentry being changed, which shares with itself by
    definition and is left out.
    """
    wanted = share_key(data)
    together = sorted(
        saved.title for saved in _others(entry, editing) if share_key(saved.data) == wanted
    )
    say = await sentences(hass)
    if together:
        return say("trial_grouped", others=", ".join(together))
    return say("trial_alone")


def _others(entry: ConfigEntry, editing: str | None) -> Iterator[ConfigSubentry]:
    """The entry's saved questions, less the one being edited."""
    for saved in entry.subentries.values():
        if saved.subentry_type == SUBENTRY_QUESTION and saved.subentry_id != editing:
            yield saved
