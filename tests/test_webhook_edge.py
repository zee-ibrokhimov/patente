"""What the edge exposes, now that a third path exists.

Until now nginx routed exactly two things: the built frontend, and the initData-
authenticated /webapp/*. Everything else on the API was unreachable BY OMISSION — no
location block, so nginx answers 404 without ever contacting the API.

`POST /webhooks/tribute` is the third, and it is the only one the original brief ever
permitted. It is safe to expose for one reason and one reason only: it is the single
endpoint that authenticates its own caller, with HMAC-SHA256 over the raw body, and
`verify()` fails closed when TRIBUTE_WEBHOOK_SECRET is unset.

These tests read the nginx config as text. That is cruder than driving a real nginx, but
it is the file that ships, and the properties worth pinning are structural: an EXACT
location match rather than a prefix, POST only, and no new hole under /webhooks/.
"""

from __future__ import annotations

import pathlib
import re

import pytest

CONF = (pathlib.Path(__file__).resolve().parent.parent
        / "webapp" / "nginx.conf").read_text(encoding="utf-8")


def block(pattern: str) -> str:
    """The body of the first location block whose header matches."""
    m = re.search(pattern + r"\s*\{", CONF)
    assert m, f"no location matching {pattern!r}"
    depth, i = 0, m.end() - 1
    for j in range(i, len(CONF)):
        if CONF[j] == "{":
            depth += 1
        elif CONF[j] == "}":
            depth -= 1
            if depth == 0:
                return CONF[i:j]
    raise AssertionError("unbalanced braces")


def test_the_webhook_is_matched_exactly_not_as_a_prefix():
    """`location = /webhooks/tribute` binds that one URI. A prefix match (`^~`) would
    hand every future /webhooks/* straight to the API, which is how a second, unintended
    endpoint becomes public without anyone editing this file again."""
    assert re.search(r"location\s*=\s*/webhooks/tribute\s*\{", CONF)
    assert not re.search(r"location\s*\^~\s*/webhooks/tribute", CONF)


def test_everything_else_under_webhooks_is_still_shut():
    assert re.search(r"location\s*\^~\s*/webhooks/\s*\{\s*return 404;", CONF)


def test_only_post_reaches_the_webhook():
    """A webhook is a POST. Anything else arriving here is a probe, and refusing it at
    the edge means it never costs an HMAC computation."""
    assert "limit_except POST { deny all; }" in block(r"location\s*=\s*/webhooks/tribute")


def test_the_webhook_is_rate_limited():
    """The URI is guessable and the signature check is cheap but not free."""
    body = block(r"location\s*=\s*/webhooks/tribute")
    assert "limit_req" in body
    assert re.search(r"limit_req_zone\s+\S+\s+zone=webhook:", CONF), \
        "the zone must be declared at http level or nginx will not start"


def test_the_dangerous_routes_are_still_unreachable():
    """The reason the API may have no authentication at all. If any of these gained a
    proxy_pass, anyone could grant themselves a paid pass or delete a user."""
    for path in ("/users", "/health", "/docs", "/redoc", "/openapi.json"):
        assert re.search(rf"location[^\n]*{re.escape(path)}[^\n]*\{{\s*return 404;", CONF), \
            f"{path} is no longer explicitly refused"


def test_there_is_no_catch_all_proxy():
    """`location / ` must serve the SPA, never the API."""
    body = block(r"location\s*/\s")
    assert "proxy_pass" not in body
    assert "try_files" in body


@pytest.mark.parametrize("path", ["/webapp/", "= /webhooks/tribute"])
def test_only_two_things_proxy_to_the_api(path):
    """Counted, so that adding a third is a deliberate act that fails this test."""
    assert CONF.count("proxy_pass http://api:8000;") == 2


def test_the_webhook_body_is_not_rewritten():
    """The HMAC covers the exact bytes Tribute sent. Anything that re-encodes the body
    breaks every signature, and the failure would look like a Tribute problem."""
    body = block(r"location\s*=\s*/webhooks/tribute")
    assert "proxy_request_buffering on;" in body
