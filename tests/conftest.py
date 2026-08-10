import hashlib
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "content"))

from api.deps import get_session  # noqa: E402
from api.main import app  # noqa: E402
from api.models import Base, Cluster, Explanation, Figure, Question, Quesito, Topic, Translation  # noqa: E402
from shared.constants import STATUS_APPROVED, STATUS_DRAFT  # noqa: E402
from shared.db import make_async_engine, make_sync_engine  # noqa: E402


@pytest.fixture
def engine(tmp_path):
    eng = make_sync_engine(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def figure_root(tmp_path):
    """A content/out/ stand-in holding two real files under images/."""
    images = tmp_path / "figures" / "images"
    images.mkdir(parents=True)
    for name, body in (("sign_a.jpeg", b"AAAA"), ("sign_b.jpeg", b"BBBB")):
        (images / name).write_bytes(body)
    return tmp_path / "figures"


def sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


# --------------------------------------------------------------------------
# API fixtures
# --------------------------------------------------------------------------

@pytest_asyncio.fixture
async def api_db(tmp_path):
    """A throwaway async database with a small, fully-formed content set.

    Two topics, four questions. Question 1 has an approved RU explanation and an RU
    translation; question 3's is still a draft, and question 4 has no cluster at all so
    nothing could ever be written for it.

    The draft used to be here to prove it was *withheld*. Since explanations are
    generated on request, the first reader of a draft is the user who asked, so a draft
    that passed every automatic gate is now served and `flagged` is what gets withheld
    (STATUS.md §13). Same fixture, opposite assertion.
    """
    url = f"sqlite+aiosqlite:///{(tmp_path / 'api.db').as_posix()}"
    engine = make_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)

    # Point the PROCESS-WIDE factory at this database too.
    #
    # Overriding `get_session` only redirects code that asks for the request's session. Any
    # code opening its own — the background channel refresh, and now the warm-field write
    # that exists because of the 2026-08-10 outage — went through `shared.db` and therefore
    # through whatever `DATABASE_URL` happens to say. In a test run that is the developer's
    # real database: those paths were writing to it, and every assertion about them was
    # being made against a file the test never looked at.
    import shared.db as shared_db

    from shared.config import settings as app_settings

    saved = (shared_db._async_engine, shared_db._async_session_factory,
             app_settings.database_url)
    app_settings.database_url = url
    shared_db._async_engine = engine
    shared_db._async_session_factory = factory
    async with factory() as s:
        s.add_all([
            Topic(id=1, name="Segnali di divieto"),
            Topic(id=2, name="Distanza di sicurezza"),
            Figure(path="images/sign_a.jpeg", sha256="aa"),
            Cluster(id=1, natural_key="t1|fig:images/a.jpeg", rule_summary="divieto di transito"),
            Cluster(id=2, natural_key="t2|txt:2", rule_summary="distanza di sicurezza"),
        ])
        await s.flush()
        s.add_all([
            Quesito(id=100, topic_id=1, primary_image="images/sign_a.jpeg"),
            Quesito(id=200, topic_id=2, primary_image=None),
        ])
        await s.flush()
        s.add_all([
            Question(id=1, quesito_id=100, topic_id=1, cluster_id=1,
                     statement_it="Il segnale raffigurato vieta il transito",
                     answer=True, image_path="images/sign_a.jpeg", source_version="v1"),
            Question(id=2, quesito_id=100, topic_id=1, cluster_id=1,
                     statement_it="Il segnale raffigurato indica un parcheggio",
                     answer=False, image_path="images/sign_a.jpeg", source_version="v1"),
            Question(id=3, quesito_id=200, topic_id=2, cluster_id=2,
                     statement_it="La distanza di sicurezza dipende dalla velocità",
                     answer=True, source_version="v1"),
            Question(id=4, quesito_id=200, topic_id=2, cluster_id=None,
                     statement_it="La distanza di sicurezza è sempre 10 metri",
                     answer=False, source_version="v1"),
        ])
        await s.flush()
        s.add_all([
            Translation(question_id=1, lang="ru", statement="Знак запрещает движение"),
            Explanation(cluster_id=1, lang="ru", text="Круглый знак с красной каймой запрещает движение.",
                        status=STATUS_APPROVED),
            # Written but not yet read by a human — must never be served.
            Explanation(cluster_id=2, lang="ru", text="ЧЕРНОВИК", status=STATUS_DRAFT),
        ])
        await s.commit()

    yield factory

    (shared_db._async_engine, shared_db._async_session_factory,
     app_settings.database_url) = saved
    await engine.dispose()


@pytest_asyncio.fixture
async def client(api_db):
    async def override():
        async with api_db() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_session] = override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def end_trial(api_db, chat_id: int) -> None:
    """Put a user past their free trial.

    Every new user is granted TRIAL_DAYS of full access at first contact, so any test
    that wants to exercise the FREE tier has to say so explicitly now. Before the trial
    existed, "just created" and "free" were the same state; they are not any more, and a
    test relying on the old equivalence silently tests a paying user instead.
    """
    from sqlalchemy import update as sa_update

    from api.models import User

    async with api_db() as session:
        await session.execute(
            sa_update(User).where(User.chat_id == chat_id).values(pass_expires_at=None)
        )
        await session.commit()


async def studied_on(api_db, chat_id: int, days: list[date], questions: int | None = None):
    """Make each of these Rome days a qualifying streak day, the way a learner would.

    Answers are written and `streak.note_answer` is then run over them in chronological
    order, rather than inserting `streak_days` rows directly. That is the difference between
    a test that proves the streak works and one that proves the test can write rows: the
    goal, the distinctness rule and the minimum gap between days are all in `note_answer`,
    and a fixture that bypasses it would keep passing after any of them broke.

    `days` are Rome dates. Question ids are unique per day, and answers land at midday Rome
    so the days are 24 hours apart and clear the minimum gap.
    """
    from api.models import Event
    from api.services import streak
    from shared.constants import EV_ANSWER_GIVEN

    questions = streak.GOAL if questions is None else questions
    async with api_db() as s:
        for day in sorted(days):
            noon = datetime.combine(day, time(12), tzinfo=streak.ROME).astimezone(timezone.utc)
            when = noon
            for i in range(questions):
                when = noon + timedelta(seconds=i * 10)
                s.add(Event(chat_id=chat_id, type=EV_ANSWER_GIVEN, created_at=when,
                            payload={"question_id": day.toordinal() * 1000 + i,
                                     "correct": True, "credited": True}))
            await s.flush()
            await streak.note_answer(s, chat_id, when)
        await s.commit()


@pytest_asyncio.fixture
async def registered(client, api_db):
    """An ESTABLISHED user: onboarded, and past the free trial.

    Every new user is granted TRIAL_DAYS of full access at first contact, so a
    freshly-created account is a PAYING-equivalent one and is the wrong baseline for the
    many tests here that exercise the free tier and the paywall. Those tests were written
    when "new" and "free" were the same thing; they are not any more.

    The trial is cleared explicitly rather than by setting TRIAL_DAYS=0 for the suite,
    because that would stop the trial itself ever being exercised. Tests that care about
    the trial create their own user and assert on it — see tests/test_trial.py.
    """
    r = await client.post("/users", json={"chat_id": 42, "lang": "ru"})
    assert r.status_code == 200
    await end_trial(api_db, 42)
    return (await client.get("/users/42")).json()


@pytest.fixture
def now():
    return datetime.now(timezone.utc)


@pytest.fixture
def listato():
    """A miniature questions.json: 2 topics, 2 quesiti, 4 statements, 2 figures."""
    return {
        "source_file": "test.pdf",
        "source_version": "2025-04-23",
        "counts": {"statements": 4, "quesiti": 2, "topics": 2, "figures": 2},
        "topics": [{"id": 1, "name": "Segnali di divieto"}, {"id": 2, "name": "Distanza di sicurezza"}],
        "quesiti": [
            {"id": 100, "topic_id": 1, "primary_image": "images/sign_a.jpeg", "statements": [1, 2]},
            {"id": 200, "topic_id": 2, "primary_image": None, "statements": [3, 4]},
        ],
        "questions": [
            {"id": 1, "quesito_id": 100, "topic": "Segnali di divieto", "topic_id": 1,
             "stem_it": None, "statement_it": "Il segnale raffigurato vieta il transito",
             "answer": True, "image": "images/sign_a.jpeg", "page": 1,
             "source_version": "2025-04-23"},
            {"id": 2, "quesito_id": 100, "topic": "Segnali di divieto", "topic_id": 1,
             "stem_it": None, "statement_it": "Il segnale (A) segue il segnale (B)",
             "answer": False, "image": "images/sign_b.jpeg", "page": 1,
             "source_version": "2025-04-23"},
            {"id": 3, "quesito_id": 200, "topic": "Distanza di sicurezza", "topic_id": 2,
             "stem_it": None, "statement_it": "La distanza di sicurezza dipende dalla velocità",
             "answer": True, "image": None, "page": 2, "source_version": "2025-04-23"},
            {"id": 4, "quesito_id": 200, "topic": "Distanza di sicurezza", "topic_id": 2,
             "stem_it": None, "statement_it": "La distanza di sicurezza è sempre 10 metri",
             "answer": False, "image": None, "page": 2, "source_version": "2025-04-23"},
        ],
    }
