"""Someone who pays you is told, and told the right thing.

THREE DEFECTS, ALL IN THE ONE PATH THIS BUSINESS RUNS ON. Payment moved off Tribute on
2026-08-09: there is no checkout page, no card form and no webhook. A learner messages the
owner, they agree terms, and the owner taps a preset in the admin console. That single
grant is the ONLY sale mechanism the product has.

1. The button passed `notify: false`, hard-coded, at its only call site. Every customer who
   paid received nothing at all. The event log shows what that cost: a `pass_granted`
   followed fourteen seconds later by a hand-typed `broadcast_sent "30days Granted!"`, and
   the same pair again days later — the owner noticing each time and patching it by hand.

2. The message it would have sent ended "Manage your subscription: @tribute". That handle
   belongs to a service that stopped taking this product's money four months earlier and
   has no record of anyone who paid since. Fixing (1) without (2) sends the first paying
   customer to a dead end.

3. Both grant paths sent "paid". A group grant takes no money from anyone — `GrantManyIn`
   has no amount field at all — so the first campaign that gifted a week would have told a
   segment of learners "✅ Payment received. Thank you." for something they were never
   charged for.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from api.services import notify
from shared.config import settings

ROOT = Path(__file__).resolve().parent.parent
MAIN = (ROOT / "webapp/src/main.ts").read_text(encoding="utf-8")
ADMIN = (ROOT / "api/routes/webapp_admin.py").read_text(encoding="utf-8")

LANGS = ("ru", "en", "it", "uz")
WHEN = __import__("datetime").datetime(2026, 10, 28, tzinfo=__import__("datetime").timezone.utc)


# --- the button actually tells them ---------------------------------------------

def _no_comments(text: str) -> str:
    """`//` lines stripped. Every argument in the call below carries a comment explaining
    itself, and a regex over the raw source matches across them and finds nothing."""
    return "\n".join(line for line in text.splitlines()
                     if not line.strip().startswith("//"))


def _grant_call() -> str:
    body = MAIN[MAIN.index("async function grantTo("):]
    body = body[:body.index("\nfunction ", 10)]
    call = body[body.index("await admin.grant("):]
    return _no_comments(call[:call.index(");") + 2])


def test_the_grant_button_notifies():
    """The defect, named. `false` at this one call site is the whole of it."""
    call = _grant_call()
    assert re.search(r",\s*true,\s*preset\.cents\)", call), \
        f"the grant is not notifying:\n{call}"
    assert "false, preset.cents" not in call, "notify is hard-coded false again"


def test_every_preset_goes_through_that_one_call():
    """The presets are a loop over one handler, so there is exactly one place this can be
    wrong — and exactly one place a future edit can quietly turn it off again."""
    body = MAIN[MAIN.index("async function grantTo("):]
    body = body[:body.index("\nfunction ", 10)]
    assert body.count("await admin.grant(") == 1, \
        "there is more than one grant call site now; both need checking"
    assert _grant_call().count("preset.cents") == 2, \
        "the call no longer passes the amount, which is what picks the message"
    # Sliced FORWARD from the presets: there are earlier `] as const;` in this file, and
    # indexing from zero produced an empty string that matched nothing and passed.
    start = MAIN.index("const GRANT_PRESETS")
    presets = MAIN[start:MAIN.index("] as const;", start)]
    assert len(re.findall(r"\{ label:", presets)) == 4, \
        f"expected four presets, found {len(re.findall(r'{ label:', presets))}"


# --- a sale is thanked, a gift is not billed ------------------------------------

def test_the_server_picks_the_message_from_the_money():
    """Not from anything the client sends. `amount_cents` already separates a sale from a
    gift everywhere else in this file — it writes the Purchase row, and `plan()` reads it
    to tell a buyer from a trialist — so a client-supplied kind would be a second source of
    truth for one question."""
    assert '"paid" if body.amount_cents else "gift"' in ADMIN


def test_a_group_grant_never_claims_it_was_paid_for():
    body = ADMIN[ADMIN.index("async def _tell_them("):]
    body = body[:body.index("await asyncio.sleep(broadcast.PAUSE)")]
    # The one call, checked directly. Scanning the whole body for the string "paid" also
    # reads the docstring, which explains what it used to say — so the test failed on its
    # own explanation of the bug.
    calls = re.findall(r"notify\.payment\([^)]*\)", body)
    assert calls == ['notify.payment(chat_id, lang, "gift", expires_at, "")'], \
        f"a gifted week is announced as: {calls}"


@pytest.mark.parametrize("lang", LANGS)
def test_the_gift_message_does_not_mention_paying(lang):
    text = notify.compose("gift", lang, WHEN, "")
    for word in ("оплат", "payment", "pagament", "to'lov qabul"):
        assert word not in text.lower(), \
            f"the {lang} gift message says {word!r}: {text!r}"


@pytest.mark.parametrize("lang", LANGS)
def test_the_paid_message_still_thanks_them(lang):
    text = notify.compose("paid", lang, WHEN, "")
    assert "28.10.2026" in text, "the expiry date is missing from the one receipt they get"


# --- nothing live points at Tribute any more ------------------------------------

@pytest.mark.parametrize("kind", ["paid", "gift", "ending", "lapsed"])
@pytest.mark.parametrize("lang", LANGS)
def test_no_reachable_money_message_mentions_tribute(kind, lang):
    """These four are the only kinds anything still calls: `paid`/`gift` from the two
    grants, `ending` and `lapsed` from the hourly lapse job. `trial` and `cancelled` are
    reachable only from the Tribute webhook, which no longer receives anything."""
    assert "tribute" not in notify.compose(kind, lang, WHEN, "").lower()


@pytest.mark.parametrize("lang", LANGS)
def test_the_delete_warning_points_at_a_live_handle(lang):
    """A pass-holder typing /delete was told to arrange refunds through @tribute."""
    import json
    text = json.loads((ROOT / f"bot/locales/{lang}.json").read_text(encoding="utf-8"))[
        "delete_paid_warning"]
    assert "tribute" not in text.lower()
    assert "{handle}" in text, "the handle is hard-coded again rather than taken from config"


def test_the_handle_comes_from_config_not_from_the_string(monkeypatch):
    """Baking it into four translations is how "@tribute" outlived Tribute by four months.
    Read at send time, it moves with the deployment."""
    monkeypatch.setattr(settings, "sales_contact", "@someone_else")
    assert "@someone_else" in notify.compose("paid", "en", WHEN, "")
    monkeypatch.setattr(settings, "sales_contact", "@third_person")
    assert "@third_person" in notify.compose("paid", "en", WHEN, "")


def test_a_deployment_with_no_contact_still_produces_a_sentence(monkeypatch):
    """The fallback is sales -> support -> nothing, and the nothing case must not leave a
    message ending in a dangling colon."""
    monkeypatch.setattr(settings, "sales_contact", "")
    monkeypatch.setattr(settings, "support_contact", "")
    text = notify.compose("paid", "en", WHEN, "")
    assert not text.rstrip().endswith(":"), f"dangling label with no handle: {text!r}"
