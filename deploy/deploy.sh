#!/usr/bin/env bash
# Deploys whatever origin/main points at, on a server (staging or prod).
# Idempotent, so re-running after a failure is always the right move.
#
#   /srv/apps/wravel-server/deploy/deploy.sh
#
# This is the whole deploy, first or hundredth. Nothing here assumes a human is
# watching -- it either finishes or exits non-zero -- so a CI job can later run
# it over ssh unchanged.

set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."

if [[ ! -f .env ]]; then
    echo "no .env in $PWD -- run deploy/newenv.sh <staging|prod> first" >&2
    exit 1
fi
if ! grep -q '^COMPOSE_PROFILES=server' .env; then
    echo ".env in $PWD is not a server one (no COMPOSE_PROFILES=server)" >&2
    exit 1
fi

# --ff-only: the server checkout is not a place to make edits, and if it has
# diverged from origin this stops rather than quietly merging.
git pull --ff-only --quiet
echo "deploying $(git rev-parse --short HEAD) to $(sed -n 's/^ENVIRONMENT=//p' .env): $(git log -1 --pretty=%s)"

# --pull refreshes the python base image too, so its security fixes arrive.
docker compose build --pull app

# Migrate and seed with the new image before the running one is replaced.
# bootstrap.py is idempotent: new migrations, plus whatever the seed lists
# gained; it never edits or removes existing rows.
docker compose up -d --wait db
docker compose run --rm --no-deps app python scripts/bootstrap.py

docker compose up -d --wait --remove-orphans

# Layers superseded by the build above; nothing in use is touched.
docker image prune -f >/dev/null

docker compose ps
