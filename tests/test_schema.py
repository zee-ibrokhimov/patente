"""Schema guarantees the rest of the system assumes without checking.

These are cheap now and expensive to discover later: a missing foreign-key
pragma makes /delete a no-op that reports success, and a missing unique
constraint turns a redelivered payment webhook into a free pass extension.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, StatementError

from api.models import Explanation, Figure, Progress, Purchase, Question, Quesito, Topic, User


def _content(session):
    # Flushed in dependency order. Quesito.primary_image and Question.image_path
    # reference figures.path without a relationship(), so SQLAlchemy's unit of
    # work does not order those inserts for us — content/seed.py flushes in the
    # same stages for the same reason.
    session.add(Topic(id=1, name="Segnali di divieto"))
    session.add(Figure(path="images/a.jpeg", sha256="deadbeef"))
    session.flush()
    session.add(Quesito(id=100, topic_id=1, primary_image="images/a.jpeg"))
    session.flush()
    session.add(Question(id=1, quesito_id=100, topic_id=1, statement_it="x",
                         answer=True, source_version="v1"))
    session.flush()


def test_foreign_keys_are_enforced(session):
    """SQLite ignores foreign keys unless the pragma is set per connection."""
    assert session.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_wal_mode_is_on(session):
    assert session.execute(text("PRAGMA journal_mode")).scalar() == "wal"


def test_question_cannot_reference_a_missing_quesito(session):
    session.add(Topic(id=1, name="t"))
    session.flush()
    session.add(Question(id=1, quesito_id=9999, topic_id=1, statement_it="x",
                         answer=True, source_version="v1"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_purchase_id_is_unique(session):
    """The webhook idempotency guarantee. A redelivered payment must not insert."""
    session.add(Purchase(chat_id=1, tribute_purchase_id="trb_1", tier="pass_1m",
                         amount_cents=299))
    session.flush()
    session.add(Purchase(chat_id=1, tribute_purchase_id="trb_1", tier="pass_1m",
                         amount_cents=299))
    with pytest.raises(IntegrityError):
        session.flush()


def test_progress_is_one_row_per_user_and_question(session):
    _content(session)
    session.add(User(chat_id=42))
    session.flush()
    session.add(Progress(chat_id=42, question_id=1, box=1, due_at=datetime.now(timezone.utc)))
    session.flush()
    session.add(Progress(chat_id=42, question_id=1, box=3, due_at=datetime.now(timezone.utc)))
    with pytest.raises(IntegrityError):
        session.flush()


def test_one_explanation_per_cluster_and_language(session):
    from api.models import Cluster

    session.add(Cluster(id=1, natural_key="t1|txt:1", rule_summary="distanza di sicurezza"))
    session.flush()
    session.add(Explanation(cluster_id=1, lang="ru", text="a"))
    session.flush()
    session.add(Explanation(cluster_id=1, lang="ru", text="b"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_delete_user_cascades_to_progress(session):
    """/delete must actually remove the answer history, not orphan it."""
    _content(session)
    session.add(User(chat_id=42))
    session.flush()
    session.add(Progress(chat_id=42, question_id=1, due_at=datetime.now(timezone.utc)))
    session.commit()

    session.delete(session.get(User, 42))
    session.commit()
    assert session.scalars(select(Progress)).all() == []
    # Content survives — only the user's data goes.
    assert session.get(Question, 1) is not None


def test_pass_expiry_is_timezone_aware(session):
    """Entitlement compares against UTC now; a naive datetime would raise."""
    expires = datetime.now(timezone.utc) + timedelta(days=30)
    session.add(User(chat_id=7, pass_expires_at=expires))
    session.commit()
    session.expunge_all()

    user = session.get(User, 7)
    assert user.pass_expires_at.tzinfo is not None
    assert user.pass_expires_at > datetime.now(timezone.utc)


def test_naive_datetimes_are_rejected(session):
    """Enforced at the boundary so "everything is UTC" stays true.

    SQLAlchemy re-raises the bind error as StatementError with the original
    ValueError chained.
    """
    session.add(User(chat_id=8, pass_expires_at=datetime(2026, 12, 1, 10, 0)))
    with pytest.raises(StatementError) as exc:
        session.flush()
    assert isinstance(exc.value.orig, ValueError)
    assert "naive datetime" in str(exc.value.orig)


def test_user_defaults_match_the_free_tier(session):
    session.add(User(chat_id=9))
    session.commit()
    session.expunge_all()

    user = session.get(User, 9)
    assert user.pass_expires_at is None       # never bought
    assert user.free_explanations_used == 0
    assert user.translations_on is True
