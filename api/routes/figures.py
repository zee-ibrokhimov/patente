"""Figure delivery and the Telegram file_id cache (plan §6.4).

A sign figure is uploaded to Telegram once, ever. After the first send Telegram
returns a file_id that can be reused in every later message, so the bot fetches
bytes from here exactly once per distinct figure and then never again. 409 figures
serve 3946 statements — without the cache, re-uploading them to thousands of users
would be the entire bandwidth bill.

The bot pulls bytes over HTTP rather than reading the images directory itself, so
it keeps no filesystem coupling to the content pipeline and works unchanged if the
two ever run in separate containers.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_session
from api.models import Figure
from shared.config import IMAGES_DIR

router = APIRouter(prefix="/figures", tags=["figures"])


class FileIdIn(BaseModel):
    file_id: str


def _resolve(name: str):
    """Reject anything that is not a plain filename directly inside IMAGES_DIR."""
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(400, "bad figure name")
    path = (IMAGES_DIR / name).resolve()
    if not path.is_file() or path.parent != IMAGES_DIR.resolve():
        raise HTTPException(404, "unknown figure")
    return path


@router.get("/{name}")
async def download(name: str):
    return FileResponse(_resolve(name), media_type="image/jpeg")


@router.put("/{name}/file-id", status_code=200)
async def cache_file_id(
    name: str, body: FileIdIn, session: AsyncSession = Depends(get_session)
):
    """Record the file_id Telegram returned for this figure's first upload."""
    figure = await session.get(Figure, f"images/{name}")
    if figure is None:
        raise HTTPException(404, "unknown figure")
    figure.telegram_file_id = body.file_id
    await session.flush()
    return {"ok": True, "path": figure.path}
