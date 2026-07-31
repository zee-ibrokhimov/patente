"""Inbound webhooks. Currently only Tribute (plan §4.1).

The route deliberately does almost nothing: it hands the **raw body** to the service and
turns the outcome into a status code. Everything else — signature, idempotency, extending
the pass — is in `api/services/purchases.py`, because a webhook and the admin grant and a
future Mini App purchase must not each hold their own opinion about what a paid pass is.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_session
from api.services import purchases

log = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/tribute")
async def tribute(
    request: Request,
    trbt_signature: str | None = Header(default=None, alias=purchases.SIGNATURE_HEADER),
    session: AsyncSession = Depends(get_session),
):
    """Money in, or money back.

    `await request.body()` rather than a Pydantic model on purpose: the HMAC covers the
    exact bytes that were sent, and letting FastAPI parse and re-serialise would break the
    comparison — see the module docstring in api/services/purchases.py.

    **A duplicate answers 200.** Redelivery is normal, and returning an error for it would
    make Tribute retry forever and eventually alert on a webhook that is working.

    Rejections are 400, never 500: a bad signature is a delivery we refuse, not a failure
    on our side worth retrying. 500 would invite Tribute to send it again.
    """
    body = await request.body()
    try:
        result = await purchases.handle(session, body, trbt_signature)
    except purchases.WebhookRejected as exc:
        log.warning("rejected a Tribute webhook: %s", exc)
        # Recorded BEFORE raising. A rejected delivery is the most interesting row in the
        # table — it is either a misconfiguration or someone probing — and a record that
        # only keeps successes cannot tell you which.
        await purchases.record_delivery(session, body, ok=False, outcome=f"rejected: {exc}")
        raise HTTPException(400, str(exc)) from exc

    await purchases.record_delivery(session, body, ok=True,
                                    outcome=str(result.get("status", "?")))
    return result
