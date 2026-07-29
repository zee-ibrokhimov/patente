from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_session, get_user
from api.models import User
from api.schemas import UserIn, UserOut, UserSettingsIn
from api.services import users
from api.services.entitlement import evaluate
from api.services.events import record
from shared.constants import EV_TRANSLATION_TOGGLED

router = APIRouter(prefix="/users", tags=["users"])


def _out(user: User) -> UserOut:
    ent = evaluate(user)
    return UserOut(
        chat_id=user.chat_id,
        lang=user.lang,
        translations_on=user.translations_on,
        pass_expires_at=user.pass_expires_at,
        has_pass=ent.has_pass,
        free_explanations_left=ent.free_explanations_left,
        onboarded_at=user.onboarded_at,
        created_at=user.created_at,
    )


@router.post("", response_model=UserOut, status_code=200)
async def create_or_get(body: UserIn, session: AsyncSession = Depends(get_session)):
    """Idempotent. /start on an existing user must not reset their progress."""
    user, _created = await users.get_or_create(session, body.chat_id, body.lang)
    return _out(user)


@router.get("/{chat_id}", response_model=UserOut)
async def read(user: User = Depends(get_user)):
    return _out(user)


@router.patch("/{chat_id}", response_model=UserOut)
async def update(
    body: UserSettingsIn,
    user: User = Depends(get_user),
    session: AsyncSession = Depends(get_session),
):
    was_on = user.translations_on
    try:
        await users.update_settings(
            session,
            user,
            lang=body.lang,
            translations_on=body.translations_on,
            onboarded=body.onboarded,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    if body.translations_on is not None and body.translations_on != was_on:
        await record(
            session, EV_TRANSLATION_TOGGLED, chat_id=user.chat_id, on=body.translations_on
        )
    return _out(user)


@router.delete("/{chat_id}", status_code=204)
async def erase(chat_id: int, session: AsyncSession = Depends(get_session)):
    """GDPR erasure. Idempotent: deleting an unknown user is still a 204."""
    await users.delete_user(session, chat_id)
    return Response(status_code=204)
