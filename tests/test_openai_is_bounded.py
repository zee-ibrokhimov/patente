"""A slow third party must not become a total outage.

Every generation path holds the request's database session across the model call —
`ensure` passes the session straight into `generate`. The SDK's default timeout is 600
seconds. SQLAlchemy's async pool is five connections plus ten overflow, so fifteen
requests waiting on a slow OpenAI exhaust it and the API stops answering ANYTHING: the
profile, the stats screen, an exam timer. A third party being slow would have taken down a
product that is mostly not about that third party.
"""

from __future__ import annotations

from api.services.explanations import OPENAI_RETRIES, OPENAI_TIMEOUT, openai_client


def test_the_client_has_an_explicit_timeout():
    """Without one the SDK waits ten minutes, holding a connection the whole time."""
    client = openai_client()
    assert client.timeout is not None
    # Was 60. Raised with the bound itself when explanations moved to gpt-5-mini at full
    # reasoning — measured 23.7s average, one cluster at 42.8s, because it emits ~5000
    # reasoning tokens nobody sees. The ceiling still has to be a number, not "eventually".
    assert float(client.timeout) <= 120, "long enough to be an outage"


def test_the_timeout_is_well_above_the_measured_call_time():
    """Cold-cache explanations were measured at 4.9s and translations at 3.8s
    (STATUS §16). A timeout near those would fire on healthy calls."""
    assert OPENAI_TIMEOUT >= 20


def test_retries_are_bounded_so_the_worst_case_is_knowable():
    """timeout x (retries + 1) is how long a connection can be held. It should be a number
    someone can reason about, not half an hour.

    Three minutes rather than two since explanations moved to a reasoning model. Affordable
    for the reason the model changed at all: explanations are PREPARED ahead on their own
    session while a start screen shows, so the connection being held is a background one
    and no learner is watching it. The bound still exists — without one a hung call holds a
    connection until the process dies.
    """
    assert OPENAI_TIMEOUT * (OPENAI_RETRIES + 1) <= 180


def test_translations_share_the_same_bounded_client():
    """They import the factory rather than building their own — so a timeout added in one
    place cannot silently miss the other path that holds a session across a model call."""
    from api.services.translations import openai_client as translations_client

    assert translations_client is openai_client
