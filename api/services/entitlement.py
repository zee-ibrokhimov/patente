"""What a given user is allowed to see, right now.

The single most important rule in the system (plan §6.3): **never trust the
client for entitlement.** Hiding an explanation in the frontend is not a paywall.
Every response that would carry a translation or an explanation is built here,
and when the pass has lapsed the field is simply absent from the payload — not
present-but-flagged, not blanked client-side.

The second rule is subtler and shows up in the UI: "you must pay to see this"
and "nobody has written this yet" are different states. Showing a paywall for
an explanation that does not exist sells something undeliverable and reads as a
bug to the user. Hence LOCKED vs UNAVAILABLE below.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timezone

from shared.config import settings
from shared.constants import (
    REQUIRE_PASS_FOR_SPACED_REPETITION,
    REQUIRE_PASS_FOR_STATS,
)


class Access(str, enum.Enum):
    SHOWN = "shown"              # here it is
    AVAILABLE = "available"      # yours if you ask — not fetched yet
    LOCKED = "locked"            # it exists, the pass does not — show the paywall
    UNAVAILABLE = "unavailable"  # not written yet — say nothing, sell nothing
    OFF = "off"                  # user switched translations off themselves


@dataclass(frozen=True)
class Entitlement:
    has_pass: bool
    pass_expires_at: datetime | None
    free_explanations_left: int

    @property
    def can_translate(self) -> bool:
        return self.has_pass

    @property
    def can_explain(self) -> bool:
        """A pass, or one of the lifetime taster explanations still unspent.

        Quality is the entire pitch, and asking someone to pay for an explanation
        whose quality they have never seen is a hard sell (plan §4.3).
        """
        return self.has_pass or self.free_explanations_left > 0

    @property
    def spends_free_explanation(self) -> bool:
        return not self.has_pass and self.free_explanations_left > 0

    @property
    def can_use_spaced_repetition(self) -> bool:
        return self.has_pass or not REQUIRE_PASS_FOR_SPACED_REPETITION

    @property
    def can_see_stats(self) -> bool:
        return self.has_pass or not REQUIRE_PASS_FOR_STATS


def evaluate(user, now: datetime | None = None, free_limit: int | None = None) -> Entitlement:
    """Snapshot of what this user may see at this instant.

    `free_limit` is configuration, not user state, so it can be widened or
    narrowed after beta without a migration or a backfill.
    """
    now = now or datetime.now(timezone.utc)
    limit = settings.free_explanations if free_limit is None else free_limit
    expires = user.pass_expires_at
    active = expires is not None and expires > now
    return Entitlement(
        has_pass=active,
        pass_expires_at=expires,
        free_explanations_left=max(0, limit - user.free_explanations_used),
    )


def translation_access(entitlement: Entitlement, user, translation_exists: bool) -> Access:
    if not user.translations_on:
        return Access.OFF
    if not translation_exists:
        return Access.UNAVAILABLE
    return Access.SHOWN if entitlement.can_translate else Access.LOCKED


def explanation_access(entitlement: Entitlement, explanation_exists: bool) -> Access:
    if not explanation_exists:
        return Access.UNAVAILABLE
    return Access.SHOWN if entitlement.can_explain else Access.LOCKED


def explanation_offer(entitlement: Entitlement, groundable: bool, withheld: bool) -> Access:
    """Whether to offer the explanation at all, before it has been fetched.

    Explanations are produced on request now, so at the moment a user answers there is
    usually nothing to show yet and no way to know whether there will be — the model
    might decline. So answering advertises an *offer*, and the bot renders a button
    rather than text.

    `groundable` is false when the question has no cluster: nothing could ever be
    written for it. `withheld` is true when a stored explanation exists but failed a
    gate, which is the one case where "nobody has written this" is known in advance and
    the button should not appear at all.

    LOCKED still comes last, so a user without a pass sees a paywall rather than
    silence for content that does in fact exist.
    """
    if not groundable or withheld:
        return Access.UNAVAILABLE
    return Access.AVAILABLE if entitlement.can_explain else Access.LOCKED
