"""What an answer shows once a question's own rules are applied to it.

The server's answer is the model's. What an entity or the mentat_decision event
shows can also depend on rules the user wrote next to the question, and on what
the last answer showed. Those rules live here, so the sensors and the event
read one judgement and cannot disagree. Nothing here sends a request or reads
Home Assistant's state.
"""

from __future__ import annotations

from dataclasses import dataclass

from jevclient import Answer, NoulAnswer

from .watches import QuestionSpec


@dataclass(frozen=True, slots=True)
class Verdict:
    """A noul's verdict, and whether it was kept from the last answer.

    `on` is None while the probability sits between release and cutoff and no
    earlier answer has said which side it was on, as after a restart.
    """

    on: bool | None
    held: bool = False


def side(probability: float, cutoff: float, release: float | None) -> bool | None:
    """True at or over the cutoff, False under the release, None in between.

    A release of None is the cutoff, which leaves nothing in between.
    """
    if probability >= cutoff:
        return True
    if probability < (cutoff if release is None else release):
        return False
    return None


def verdict(spec: QuestionSpec, answer: Answer | None, previous: bool | None) -> Verdict | None:
    """The verdict for this answer, or None when the question gives none.

    On at or over the cutoff and off under the release. In between, the verdict
    the last answer gave stands, so a probability that wobbles around one number
    does not flip the sensor on every evaluation. A cutoff of 0 is a cutoff, so
    only a missing one means no verdict.
    """
    if spec.cutoff is None or not isinstance(answer, NoulAnswer):
        return None
    on = side(answer.noul, spec.cutoff, spec.release)
    return Verdict(on=previous, held=True) if on is None else Verdict(on=on)
