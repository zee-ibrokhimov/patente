# Python 3.12 — the version DEPLOY_PROMPT.md names as tested.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# The `content` extra is required, not optional: seed.py and cluster.py need
# rapidfuzz + numpy, and the entrypoint below runs them. `dev` is omitted.
COPY pyproject.toml ./
COPY api ./api
COPY bot ./bot
COPY shared ./shared
RUN pip install --upgrade pip setuptools wheel && pip install -e ".[content]"

# content/ is a directory of scripts, not a package, so it is copied after the
# install. content/out/ is READ AT RUNTIME and must not be pruned as a build
# artefact: the legal corpus lives in content/out/norms/*.json and the 409
# figures in content/out/images/ (DEPLOY_PROMPT.md).
COPY content ./content
COPY ops ./ops
# alembic.ini points script_location at api/migrations, copied above.
COPY alembic.ini ./

# Bring the database up to date before starting whatever we were asked to run.
# Only the API does this: the bot holds no database handle (plan §6.1) and two
# processes racing alembic on one SQLite file is a "database is locked" waiting
# to happen, so the bot service sets PATENTE_SKIP_MIGRATE=1.
#
# seed.py is diff-based and cluster.py matches on clusters.natural_key, so both
# are idempotent — a rerun reports "0 new, 3382 kept, 0 removed" rather than
# rebuilding. Re-running clustering after explanations exist would once have
# deleted every one of them (STATUS.md §10); the natural key is what makes this
# safe to run on every deploy.
RUN printf '%s\n' \
    '#!/bin/sh' \
    'set -e' \
    'if [ "${PATENTE_SKIP_MIGRATE}" != "1" ]; then' \
    '  echo "[entrypoint] alembic upgrade head"' \
    '  python -m alembic upgrade head' \
    '  if [ "${PATENTE_SKIP_SEED}" != "1" ]; then' \
    '    echo "[entrypoint] seeding content"' \
    '    python content/seed.py' \
    '    echo "[entrypoint] clustering"' \
    '    python content/cluster.py --strategy figure --write' \
    '  fi' \
    'fi' \
    'exec "$@"' \
    > /usr/local/bin/docker-entrypoint.sh \
 && chmod +x /usr/local/bin/docker-entrypoint.sh

# The database lives on a VOLUME mounted here. It holds every user's progress and
# entitlement plus the explanations and translations that cost money to generate —
# none of it reconstructible. It must never live in the container layer.
RUN mkdir -p /data && useradd --system --uid 999 patente && chown -R patente:patente /app /data
USER patente

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
