"""The Mini App's only defence. Plan §6.3: without this, anyone can craft a request
claiming any Telegram ID and unlock paid content for free."""

from __future__ import annotations

import json
import time

import pytest

from api.services import telegram_auth
from api.services.telegram_auth import InitDataRejected, verify
from shared.config import settings

TOKEN = "8918020834:AAEtest-token-not-real-only-for-tests"


def make(token: str = TOKEN, *, chat_id: int = 357127133, auth_date: int | None = None,
         **extra) -> str:
    fields = {
        "user": json.dumps(
            {"id": chat_id, "first_name": "Zee", "language_code": "ru"},
            separators=(",", ":"),
        ),
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": "AAHtest",
        **extra,
    }
    return telegram_auth.sign(fields, token)


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(settings, "bot_token_prod", TOKEN)
    monkeypatch.setattr(settings, "env", "prod")


def test_a_genuine_payload_yields_the_signed_for_user():
    user = verify(make())
    assert user.chat_id == 357127133
    assert user.language_code == "ru"


def test_a_forged_signature_is_refused():
    """The whole point: someone else's bot token must not produce a valid blob."""
    with pytest.raises(InitDataRejected, match="signature"):
        verify(make(token="1111111111:AAEsomeone-elses-bot-token"))


def test_tampering_with_the_user_id_breaks_the_hash():
    """Claiming another chat_id is the attack — read their data, or spend their pass."""
    good = make(chat_id=357127133)
    tampered = good.replace("357127133", "999999999")
    assert tampered != good
    with pytest.raises(InitDataRejected, match="signature"):
        verify(tampered)


def test_a_payload_with_no_hash_is_refused():
    with pytest.raises(InitDataRejected, match="no hash"):
        verify("user=%7B%22id%22%3A1%7D&auth_date=1")


def test_an_empty_payload_is_refused():
    with pytest.raises(InitDataRejected, match="no initData"):
        verify("")


def test_a_stale_payload_is_refused():
    """A signature that never expires is a permanent credential for that account."""
    old = int(time.time()) - telegram_auth.MAX_AGE_SECONDS - 60
    with pytest.raises(InitDataRejected, match="stale"):
        verify(make(auth_date=old))


def test_a_payload_from_the_future_is_refused():
    ahead = int(time.time()) + telegram_auth.MAX_AGE_SECONDS + 60
    with pytest.raises(InitDataRejected, match="future"):
        verify(make(auth_date=ahead))


def test_a_payload_just_inside_the_window_is_accepted():
    recent = int(time.time()) - telegram_auth.MAX_AGE_SECONDS + 60
    assert verify(make(auth_date=recent)).chat_id == 357127133


def test_no_bot_token_refuses_everything(monkeypatch):
    """Mirrors the Tribute webhook: unconfigured means refuse, never wave through."""
    monkeypatch.setattr(settings, "bot_token_prod", "")
    with pytest.raises(InitDataRejected, match="not configured"):
        verify(make())


def test_extra_signed_fields_take_part_in_the_hash():
    """Telegram adds fields over time; unknown ones must still be covered, not dropped."""
    blob = make(chat_id=42, start_param="deeplink", chat_type="private")
    assert verify(blob).chat_id == 42
    with pytest.raises(InitDataRejected, match="signature"):
        verify(blob.replace("deeplink", "tampered"))


def test_a_missing_user_object_is_refused():
    fields = {"auth_date": str(int(time.time())), "query_id": "AAH"}
    with pytest.raises(InitDataRejected, match="user object"):
        verify(telegram_auth.sign(fields, TOKEN))
