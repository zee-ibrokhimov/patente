"""FastAPI dependencies."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import Depends, HTTPException, Path
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import User
from shared.db import async_session_factory


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    factory = async_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_user(
    chat_id: int = Path(...), session: AsyncSession = Depends(get_session)
) -> User:
    user = await session.get(User, chat_id)
    if user is None:
        raise HTTPException(404, "unknown user — POST /users first")
    return user
