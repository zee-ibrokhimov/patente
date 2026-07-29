"""Leitner scheduling. Pure functions — no session, no clock of its own.

Kept free of I/O so the scheduling rules can be tested directly and so both
surfaces provably share one implementation. The moment the bot computes a box
itself, the two disagree within a month.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from shared.constants import FIRST_BOX, LEITNER_BOXES, LEITNER_INTERVALS_MINUTES


def interval(box: int) -> timedelta:
    return timedelta(minutes=LEITNER_INTERVALS_MINUTES[box])


def next_box(box: int, correct: bool) -> int:
    """Right answer promotes one box; wrong drops to box 1 regardless of height.

    Demoting by a single box would let a question the user keeps missing hover
    near the top and reappear a week later, which is the opposite of the point.
    """
    if not correct:
        return FIRST_BOX
    return min(box + 1, LEITNER_BOXES)


def schedule(box: int, correct: bool, now: datetime) -> tuple[int, datetime]:
    """(current box, was it right, now) -> (new box, when it is next due)."""
    box = next_box(box, correct)
    return box, now + interval(box)
