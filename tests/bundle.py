"""Reading the bundle that actually ships, and refusing to read a stale one.

Two tests here and in test_theme_is_light_only.py assert things about the built output.
Both were silently useless: `webapp/dist` is written by a LOCAL `npm run build`, but the
image is built inside Docker, so the file on disk can be hours older than the source it
claims to verify. A test that reads a stale artefact does not fail — it passes, which is
worse than not existing, because it reports coverage it does not have.

So `bundle()` compares the artefact's timestamp against every source that feeds it and
SKIPS rather than passes when it is behind. Refresh it with:

    docker build -f webapp/Dockerfile -t patente-web-check .
    CID=$(docker create patente-web-check)
    docker cp "$CID:/usr/share/nginx/html/." webapp/dist/
    docker rm "$CID"
"""

from __future__ import annotations

import pathlib

import pytest

WEB = pathlib.Path(__file__).resolve().parent.parent / "webapp"
SOURCES = ("src", "index.html", "package.json")


def newest_source_mtime() -> float:
    newest = 0.0
    for name in SOURCES:
        path = WEB / name
        if path.is_dir():
            for f in path.rglob("*"):
                if f.is_file():
                    newest = max(newest, f.stat().st_mtime)
        elif path.exists():
            newest = max(newest, path.stat().st_mtime)
    return newest


def bundle(suffix: str = "*.js") -> str:
    """The shipped bundle, or a skip. Never a pass on stale output."""
    files = sorted((WEB / "dist/assets").glob(suffix))
    if not files:
        pytest.skip("no build output — see tests/bundle.py for how to produce it")
    built = files[-1]
    if built.stat().st_mtime < newest_source_mtime():
        pytest.skip(
            f"{built.name} is older than the sources it is built from — rebuild before "
            "trusting this assertion (see tests/bundle.py)"
        )
    return built.read_text(encoding="utf-8")
