"""Core API — owns all business logic.

The bot and the Mini App are thin clients over this. Neither holds a database
handle and neither decides entitlement; if either did, the Leitner rules would be
implemented twice inside a month and the two would disagree (plan §6.1).
"""

from __future__ import annotations

from fastapi import Depends, FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_session
from api.models import Question
from api.routes import admin, webapp_admin, figures, quiz, users, webapp, webhooks
from shared.config import settings

app = FastAPI(
    title="Patente Quiz API",
    version="0.1.0",
    summary="Question selection, Leitner scheduling, entitlement and events.",
)

app.include_router(admin.router)
app.include_router(users.router)
app.include_router(quiz.router)
app.include_router(figures.router)
app.include_router(webhooks.router)
# The only routers above that may ever be reached from the internet are webhooks
# (HMAC-signed) and webapp (initData-signed). users/quiz/figures take identity from
# the URL and must stay private — see api/routes/webapp.py.
app.include_router(webapp.router)
# The owner's console. Same public prefix, same Telegram-signed authentication, and every
# route behind a staff dependency — see api/routes/webapp_admin.py for why it lives here
# rather than on a domain of its own.
app.include_router(webapp_admin.router)


@app.get("/health", tags=["ops"])
async def health(session: AsyncSession = Depends(get_session)) -> dict:
    """Liveness plus the one number that says whether content is actually loaded."""
    questions = await session.scalar(select(func.count(Question.id)))
    return {
        "status": "ok",
        "env": settings.env,
        "questions": questions or 0,
        "seeded": bool(questions),
    }
