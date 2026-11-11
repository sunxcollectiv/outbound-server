"""Bring a database to a usable state: the schema, then the area taxonomy.

For a fresh server (or a fresh local database) this is the one command to run
once DATABASE_URL points at it:

    uv run python scripts/bootstrap.py

It runs, in order:

  1. Alembic migrations, to head.
  2. Countries and territories   (app.seeds.countries)
  3. Cities                      (app.seeds.cities)
  4. Islands and regions         (app.seeds.regions)

Every step is idempotent, so it is safe to re-run after each deploy: it applies
any new migrations and adds whatever the seed lists have gained, and never edits
or removes a row that is already there.

Deliberately not done here, because each needs a secret or a decision:

  - A service token for the /admin APIs:  scripts/issue_service_token.py
  - Area cover photos (UNSPLASH_ACCESS_KEY, rate-limited):
                                          scripts/backfill_area_photos.py

Places are not seeded at all: they are created from Google as people add them.

Runs against DATABASE_URL (the dev database by default), NOT the test database.
Pass --skip-migrations when the deploy pipeline already runs Alembic.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import func, make_url, select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Area
from app.seeds.cities import seed_cities
from app.seeds.countries import seed_areas
from app.seeds.regions import seed_regions

ROOT = Path(__file__).resolve().parent.parent


def migrate() -> None:
    """Upgrade the schema to head. Paths are anchored to the repo, so this works
    from any working directory."""
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "head")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate the database and seed the area taxonomy. Idempotent."
    )
    parser.add_argument(
        "--skip-migrations",
        action="store_true",
        help="Seed only; assume the schema is already at head.",
    )
    args = parser.parse_args()

    url = make_url(get_settings().database_url)
    print(f"Database: {url.render_as_string(hide_password=True)}")

    if args.skip_migrations:
        print("Migrations: skipped")
    else:
        migrate()
        print("Migrations: at head")

    unresolved = 0
    with SessionLocal() as session:
        # Order matters: cities and regions hang off countries, and regions skip
        # any name a city already uses under the same country.
        for label, seed in (
            ("Countries and territories", seed_areas),
            ("Cities", seed_cities),
            ("Islands and regions", seed_regions),
        ):
            result = seed(session)
            line = f"{label}: created {result['created']}, already present {result['skipped']}"
            if result.get("unresolved"):
                unresolved += result["unresolved"]
                line += f", SKIPPED {result['unresolved']} (parent country not found)"
            print(line)

        by_kind = session.execute(
            select(func.coalesce(Area.kind, "(none)"), func.count())
            .group_by(Area.kind)
            .order_by(func.count().desc())
        ).all()

    total = sum(count for _, count in by_kind)
    print(f"Areas now: {total} (" + ", ".join(f"{kind} {count}" for kind, count in by_kind) + ")")

    if unresolved:
        sys.exit(f"{unresolved} seed entries had no parent country: the seed lists disagree.")

    print()
    print("Not done here:")
    print('  - Admin access:  uv run python scripts/issue_service_token.py "<name>"')
    print("  - Area photos:   uv run python scripts/backfill_area_photos.py  (needs UNSPLASH_ACCESS_KEY)")


if __name__ == "__main__":
    main()
