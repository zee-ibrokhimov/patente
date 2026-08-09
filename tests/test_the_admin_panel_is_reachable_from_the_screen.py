"""Every admin capability has a control the owner can actually tap.

The endpoints for deleting a user, deleting a link, and attaching an image or buttons to a
newsletter all shipped with no UI whatsoever. The API was complete, the tests were green,
and the panel on screen could do none of it — reported as "via mini app it is not visible".

That is a gap no server-side test can see, because from the API's point of view nothing is
wrong. So this file checks the other half: for each capability, the client both DEFINES the
call and CALLS it from a rendered control.

`api.ts` defining `deleteUser` proves nothing on its own. That is precisely the state that
shipped.
"""

from __future__ import annotations

import pathlib
import re

import pytest

WEB = pathlib.Path(__file__).resolve().parent.parent / "webapp" / "src"
API = (WEB / "api.ts").read_text(encoding="utf-8")
MAIN = (WEB / "main.ts").read_text(encoding="utf-8")


# (capability, the api.ts member, what main.ts must call)
CAPABILITIES = [
    ("take access back", "revoke", "admin.revoke("),
    ("grant to a group", "grantMany", "admin.grantMany("),
    ("remove a user", "deleteUser", "admin.deleteUser("),
    ("remove a link", "deleteLink", "admin.deleteLink("),
    ("read reports", "reports", "admin.reports("),
    ("resolve a report", "resolveReport", "admin.resolveReport("),
    ("rewrite a reported explanation", "regenerateReported", "admin.regenerateReported("),
    ("message one learner", "message", "admin.message("),
    ("grant access", "grant", "admin.grant("),
    ("send a newsletter", "broadcast", "admin.broadcast("),
]


@pytest.mark.parametrize("what,member,call", CAPABILITIES,
                         ids=[c[0].replace(" ", "-") for c in CAPABILITIES])
def test_the_capability_is_wired_to_a_control(what: str, member: str, call: str):
    assert f"{member}:" in API, f"api.ts has no {member} — cannot {what}"
    assert call in MAIN, (
        f"nothing on screen calls {call} — the owner cannot {what} from the app, "
        f"however complete the endpoint is")


def test_a_destructive_control_asks_first():
    """Deleting a learner cannot be undone, and `ask` is used rather than window.confirm
    because some Android Telegram clients suppress the browser dialog inside the webview."""
    start = MAIN.index("admin.deleteUser(")
    block = MAIN[max(0, start - 900):start]
    assert "await ask(" in block, "delete user does not confirm before destroying data"


def test_the_newsletter_can_carry_an_image_and_buttons():
    """The two fields that make an offer possible. Sending `photo_url` and `buttons` from
    the composer is the whole point — the endpoint accepted them for a week while the form
    had nowhere to type them."""
    start = MAIN.index("admin.broadcast(")
    block = MAIN[start:start + 500]
    assert "photo_url" in block, "the composer never sends an image"
    assert "buttons" in block, "the composer never sends buttons"


def test_the_offer_button_opens_the_mini_app():
    """`webapp: true` is what lands a reader on the paywall in one tap instead of in a
    browser. A plain url button would look identical in the form and be worth much less."""
    assert re.search(r"webapp:\s*true", MAIN), \
        "no control produces a Mini App button, so an offer cannot open the app"


def test_the_preview_and_the_send_use_the_same_filter():
    """The server refuses a send whose confirmed count does not match what it just
    reported. If the preview filtered on everyone and the send on subscribers only, the
    confirmed number describes a different population and every send fails — correctly, and
    incomprehensibly."""
    preview = MAIN.index("admin.previewBroadcast(")
    send = MAIN.index("admin.broadcast(", preview)
    both = MAIN[preview:send + 400]
    filters = re.findall(r"premium_only:\s*([A-Za-z_.\w]+)", both)
    assert len(filters) == 2, f"expected a filter on both calls, found {filters}"
    assert filters[0] == filters[1], (
        f"preview filters on {filters[0]} but the send filters on {filters[1]} — "
        f"the confirmed count would describe a different population")
