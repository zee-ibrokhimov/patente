import hashlib
import sys
from datetime import datetime, timezone
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

    Two topics, four questions. Question 1 has an approved RU explanation and an
    RU translation; question 3's explanation is still a draft, which is how the
    "written but not reviewed" case gets exercised — it must read as unavailable,
    never as locked.
    """
    engine = make_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'api.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add_all([
            Topic(id=1, name="Segnali di divieto"),
            Topic(id=2, name="Distanza di sicurezza"),
            Figure(path="images/sign_a.jpeg", sha256="aa"),
            Cluster(id=1, rule_summary="divieto di transito"),
            Cluster(id=2, rule_summary="distanza di sicurezza"),
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


@pytest_asyncio.fixture
async def registered(client):
    """A user who has completed /start."""
    r = await client.post("/users", json={"chat_id": 42, "lang": "ru"})
    assert r.status_code == 200
    return r.json()


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
