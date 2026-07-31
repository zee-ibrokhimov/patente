"""Which images the janitor may delete.

This is disk-space housekeeping, which sounds unimportant until it is the thing that took
four hostnames offline. On 2026-07-31 unpruned Coolify images filled a 30 GB disk, every
container died, and the Cloudflare tunnel connector died with them.

The danger in the fix is larger than the danger in the problem. `docker image prune -a`
removes every image not attached to a RUNNING container — and on that day three apps were
stopped *because* the disk was full, so the blunt fix would have deleted the only copy of
their images while trying to help.

Hence a pure function with the safety property stated explicitly, and tests that would
fail if it were ever loosened into "prune everything unused".
"""

from __future__ import annotations

import pytest

from ops.janitor import is_app_image, is_critical, removable, should_act

APP = "rboj5u2xk5dj4o4yto89ablh_api"
OTHER_APP = "kctqkbhm06yi3ux1oavdowof_web"


def img(image_id: str, repository: str = APP) -> dict:
    return {"id": image_id, "repository": repository}


# --- the safety property ----------------------------------------------------

def test_an_image_used_by_a_stopped_container_is_never_removed():
    """THE test. `in_use` comes from `docker ps -a`, so a container that is merely
    stopped still protects its image. On the day this was written, three apps were
    stopped because the disk was full — pruning "unused" images would have destroyed
    exactly the images needed to bring them back."""
    assert removable([img("sha-old")], in_use={"sha-old"}) == []


def test_an_image_no_container_references_is_removable():
    assert removable([img("sha-stale")], in_use={"sha-live"}) == ["sha-stale"]


def test_only_coolify_application_images_are_touched():
    """Base images, databases, manually pulled tools — all belong to someone else.
    cloudflared lives here too, and deleting it during an outage would be perfect."""
    others = [
        img("a", "cloudflare/cloudflared"),
        img("b", "postgres"),
        img("c", "nginx"),
        img("d", "ghcr.io/someone/thing"),
    ]
    assert removable(others, in_use=set()) == []


def test_a_second_application_is_cleaned_too():
    """The janitor serves the box, not one project — every Coolify app leaks images."""
    assert removable([img("x", OTHER_APP)], in_use=set()) == ["x"]


def test_nothing_is_removed_when_everything_is_in_use():
    images = [img("a"), img("b", OTHER_APP)]
    assert removable(images, in_use={"a", "b"}) == []


def test_an_image_with_no_id_is_skipped():
    assert removable([{"id": "", "repository": APP}], in_use=set()) == []


@pytest.mark.parametrize("repository, expected", [
    ("rboj5u2xk5dj4o4yto89ablh_api", True),
    ("rboj5u2xk5dj4o4yto89ablh_web", True),
    ("kc8v4o86jpvigq9qe4sitndr_bot", True),
    ("cloudflare/cloudflared", False),
    ("postgres", False),
    ("nginx", False),
    ("", False),
    ("short_api", False),          # too short to be a Coolify uuid
    ("<none>", False),             # a dangling image; `docker image prune` owns those
])
def test_only_uuid_named_repositories_count_as_app_images(repository, expected):
    assert is_app_image(repository) is expected


# --- when to act ------------------------------------------------------------

@pytest.mark.parametrize("used, expected", [(0, False), (59, False), (60, True), (95, True)])
def test_pruning_starts_well_before_anything_breaks(used, expected):
    """60%, not 90%. Acting at 90% means acting during an incident; the images are
    rebuilt from a Dockerfile, so there is nothing to hoard."""
    assert should_act(used) is expected


@pytest.mark.parametrize("used, expected", [(60, False), (84, False), (85, True)])
def test_the_alert_threshold_is_higher_than_the_action_threshold(used, expected):
    """An alert that fires every time the janitor does its ordinary job is one nobody
    reads by the second week."""
    assert is_critical(used) is expected


def test_acting_and_alerting_are_different_decisions():
    """If these were the same number, either the janitor would wait for an emergency or
    every routine cleanup would page someone."""
    assert should_act(70) and not is_critical(70)
