"""Leitner scheduling rules.

Pure functions, so these are the cheapest tests in the suite and cover the rules
both surfaces depend on.
"""

from datetime import datetime, timedelta, timezone

import pytest

from api.services.leitner import interval, next_box, schedule
from shared.constants import LEITNER_BOXES, LEITNER_INTERVALS_MINUTES

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("box", range(1, LEITNER_BOXES + 1))
def test_wrong_answer_always_drops_to_box_one(box):
    """Demoting by one would let a repeatedly-missed question hover near the top."""
    assert next_box(box, correct=False) == 1


@pytest.mark.parametrize("box,expected", [(1, 2), (2, 3), (3, 4), (4, 5), (5, 5)])
def test_correct_answer_promotes_one_box_and_caps(box, expected):
    assert next_box(box, correct=True) == expected


def test_intervals_grow_monotonically():
    minutes = [LEITNER_INTERVALS_MINUTES[b] for b in range(1, LEITNER_BOXES + 1)]
    assert minutes == sorted(minutes)
    assert len(set(minutes)) == len(minutes)


def test_missed_question_returns_within_the_session():
    """Ten minutes: back this session, but with other questions in between."""
    assert interval(1) == timedelta(minutes=10)
    assert timedelta(minutes=1) < interval(1) < timedelta(hours=1)


def test_schedule_returns_box_and_due_time():
    box, due = schedule(3, correct=True, now=NOW)
    assert box == 4
    assert due == NOW + timedelta(minutes=LEITNER_INTERVALS_MINUTES[4])


def test_wrong_answer_reschedules_sooner_than_a_right_one():
    _, due_wrong = schedule(3, correct=False, now=NOW)
    _, due_right = schedule(3, correct=True, now=NOW)
    assert due_wrong < due_right


def test_a_settled_question_is_pushed_a_month_out():
    box, due = schedule(5, correct=True, now=NOW)
    assert box == LEITNER_BOXES
    assert due - NOW >= timedelta(days=30)
