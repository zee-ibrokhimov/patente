"""Deciding which Docker images are safe to delete.

Coolify never prunes. Every deploy leaves a full image behind, and on 2026-07-31 fifteen
small deploys in one afternoon left 64 images at ~587 MB — about 25 GB — which filled a
30 GB disk shared with every other project on the box. Containers died, the Cloudflare
tunnel connector went with them, and four hostnames were dark until the owner fetched a
token only they could reach.

The blunt fix, `docker image prune -a`, is wrong here and dangerously so: it removes every
image not attached to a RUNNING container, and on a box where some apps are legitimately
stopped that deletes the only copy of their image. Today it would have destroyed images
for three apps that were down precisely because the disk was full.

So the rule is narrower and stated as a pure function, testable without a Docker daemon:

    remove an image only if it belongs to a Coolify application AND no container —
    running or stopped — references it.

`in_use` must come from `docker ps -a`, not `docker ps`. That distinction is the whole
safety property, and there is a test named after it.
"""

from __future__ import annotations

import re

# Coolify names an application's images after its UUID: <uuid>_<service>:<sha>. Anything
# not matching this is somebody else's — a base image, a database, a manually pulled
# tool — and is never a candidate.
COOLIFY_IMAGE = re.compile(r"^[a-z0-9]{20,}_[a-z0-9-]+$")


def is_app_image(repository: str) -> bool:
    """Whether this repository belongs to a Coolify application."""
    return bool(COOLIFY_IMAGE.match(repository.strip()))


def removable(images: list[dict], in_use: set[str]) -> list[str]:
    """Image ids that are safe to delete.

    `images`  — one dict per image with at least `id` and `repository`.
    `in_use`  — every image id referenced by ANY container, running or not.

    Returns ids, not names: an image can carry several tags and deleting by name only
    drops the tag.
    """
    doomed = []
    for image in images:
        image_id = str(image.get("id", "")).strip()
        repository = str(image.get("repository", "")).strip()
        if not image_id or image_id in in_use:
            continue
        if not is_app_image(repository):
            continue
        doomed.append(image_id)
    return doomed


def should_act(percent_used: int, threshold: int = 60) -> bool:
    """Whether to prune at all.

    Deliberately well below the point where anything breaks. Waiting for 90% means acting
    during an incident; acting at 60% means the incident does not happen. Pruning is
    cheap — the images are rebuilt from a Dockerfile — so there is no reason to hoard.
    """
    return percent_used >= threshold


def is_critical(percent_used: int, threshold: int = 85) -> bool:
    """Whether to alert a human because pruning was not enough.

    Separate from should_act on purpose: an alert that fires every time the janitor does
    its ordinary job is an alert nobody reads by the second week.
    """
    return percent_used >= threshold
