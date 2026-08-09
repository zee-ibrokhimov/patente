"""The admin panel can read what learners report, and remove things.

REPORTS. The "this explanation is wrong" button has shipped for a long time and nothing has
ever read the table. That is the worst state this particular feature can be in: it invites
someone to tell you the app is wrong and then discards what they said. Two reports were
sitting unread in production when this was written.

REMOVING. Deleting is the only destructive thing in the panel, so both deletes are narrower
than they look — staff cannot delete themselves, purchases outlive the account that made
them, and a referral code that somebody actually came through cannot be erased at all.

NEWSLETTER BUTTONS. An inline button is a place thousands of people are sent at once, which
makes it the one part of a newsletter worth validating rather than trusting.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from api.models import Purchase, ReferralLink, Report, User
from api.services import broadcast
# `_staff` is imported for its side effect: it is an autouse fixture that makes OWNER the
# only staff account and pins the bot token `auth()` signs with. Without it every request
# here is a 401, which looks like a broken route rather than a missing fixture.
from tests.test_admin_panel import _staff, auth  # noqa: F401


OWNER = 42


async def a_report(api_db, question_id: int = 1, chat_id: int = 500) -> int:
    async with api_db() as s:
        row = Report(chat_id=chat_id, question_id=question_id, cluster_id=None, lang="ru")
        s.add(row)
        await s.commit()
        return row.id


# --- the report queue --------------------------------------------------------

async def test_reports_are_readable_at_all(client, registered, api_db):
    """THE gap. Reports were written and never surfaced anywhere."""
    await a_report(api_db)
    r = await client.get("/webapp/admin/reports", headers=auth())
    assert r.status_code == 200
    body = r.json()
    assert body["open"] == 1
    assert len(body["reports"]) == 1


async def test_a_report_carries_the_statement_it_is_about(client, registered, api_db):
    """A report is only actionable next to the text being reported. Having to look the
    question up by hand is exactly the friction that leaves a queue unread."""
    await a_report(api_db, question_id=1)
    row = (await client.get("/webapp/admin/reports", headers=auth())).json()["reports"][0]
    assert row["statement"], "the report does not say which statement it is about"
    assert row["question_id"] == 1


async def test_resolving_takes_it_off_the_queue(client, registered, api_db):
    report_id = await a_report(api_db)
    r = await client.post(f"/webapp/admin/reports/{report_id}/resolve", headers=auth())
    assert r.status_code == 200

    body = (await client.get("/webapp/admin/reports", headers=auth())).json()
    assert body["open"] == 0
    assert body["reports"] == []

    # ...but it is still readable when asked for explicitly. A resolved complaint is the
    # evidence that a correction was needed, which is the only quality signal this app has.
    seen = (await client.get("/webapp/admin/reports?unresolved=false", headers=auth())).json()
    assert len(seen["reports"]) == 1


async def test_resolving_twice_is_not_an_error(client, registered, api_db):
    """Two taps on a slow connection is the realistic way this gets used."""
    report_id = await a_report(api_db)
    first = await client.post(f"/webapp/admin/reports/{report_id}/resolve", headers=auth())
    second = await client.post(f"/webapp/admin/reports/{report_id}/resolve", headers=auth())
    assert second.status_code == 200
    assert second.json()["resolved_at"] == first.json()["resolved_at"], \
        "the resolution timestamp moved on a repeat tap"


async def test_the_queue_is_staff_only(client, registered, api_db):
    """404, not 403 — an admin panel should not confirm its own existence to a stranger."""
    from api.routes import webapp_admin

    r = await client.get("/webapp/admin/reports")
    assert r.status_code in (401, 404), r.status_code
    assert webapp_admin.staff_user  # the gate this relies on


# --- removing a user ---------------------------------------------------------

async def test_a_user_can_be_removed(client, registered, api_db):
    async with api_db() as s:
        s.add(User(chat_id=9100, lang="ru"))
        await s.commit()

    r = await client.delete("/webapp/admin/users/9100", headers=auth())
    assert r.status_code == 200
    async with api_db() as s:
        assert await s.get(User, 9100) is None


async def test_staff_cannot_delete_themselves(client, registered, api_db):
    """There is no second way into this panel. Deleting the only staff account locks it
    for good, and nothing in the app can undo that."""
    r = await client.delete(f"/webapp/admin/users/{OWNER}", headers=auth())
    assert r.status_code == 409
    async with api_db() as s:
        assert await s.get(User, OWNER) is not None


async def test_deleting_a_user_keeps_their_purchases(client, registered, api_db):
    """Money that changed hands outlives the account. A refund request or a tax question
    arrives after the person is gone, and deleting the row does not undo the payment."""
    async with api_db() as s:
        s.add(User(chat_id=9200, lang="ru"))
        # `id` is an autoincrement int; the synthetic reference lives in
        # tribute_purchase_id, which is the UNIQUE column webhook redelivery keys on.
        s.add(Purchase(chat_id=9200, tribute_purchase_id="manual:9200:test",
                       tier="month", amount_cents=999, currency="EUR"))
        await s.commit()

    r = await client.delete("/webapp/admin/users/9200", headers=auth())
    assert r.status_code == 200
    assert r.json()["purchases_kept"] == 1

    async with api_db() as s:
        kept = await s.scalars(select(Purchase).where(Purchase.chat_id == 9200))
        assert len(list(kept)) == 1, "the payment record went with the account"


# --- newsletter buttons ------------------------------------------------------

def test_a_webapp_button_opens_the_mini_app(monkeypatch):
    from shared.config import settings
    monkeypatch.setattr(settings, "webapp_url", "https://patente.zeehub.xyz")
    got = broadcast.buttons_for([{"text": "Offerta", "webapp": True}])
    assert got == [{"text": "Offerta", "web_app": {"url": "https://patente.zeehub.xyz"}}]


def test_a_webapp_button_without_a_url_configured_is_refused(monkeypatch):
    """It would render as a button that opens nothing, to everybody, at once."""
    from shared.config import settings
    monkeypatch.setattr(settings, "webapp_url", "")
    with pytest.raises(ValueError, match="WEBAPP_URL"):
        broadcast.buttons_for([{"text": "Offerta", "webapp": True}])


def test_a_chat_button_becomes_a_t_me_link():
    got = broadcast.buttons_for([{"text": "Scrivimi", "chat": "@iambrock"}])
    assert got == [{"text": "Scrivimi", "url": "https://t.me/iambrock"}]


@pytest.mark.parametrize("bad", [
    "javascript:alert(1)",
    "tg://resolve?domain=x",
    "http://insecure.example",
])
def test_only_https_urls_are_accepted(bad):
    """`tg://` is the sharp one: Telegram renders it and it can act on the reader's own
    account. `javascript:` and plain http are refused for the ordinary reasons."""
    with pytest.raises(ValueError):
        broadcast.buttons_for([{"text": "Tap", "url": bad}])


def test_buttons_are_capped():
    with pytest.raises(ValueError, match="at most"):
        broadcast.buttons_for([{"text": f"b{i}", "url": "https://x.example"} for i in range(4)])


def test_no_buttons_is_fine():
    assert broadcast.buttons_for(None) == []
    assert broadcast.buttons_for([]) == []
