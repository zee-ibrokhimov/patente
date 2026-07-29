import hashlib
import sys
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "content"))

from api.models import Base  # noqa: E402
from shared.db import make_sync_engine  # noqa: E402


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
