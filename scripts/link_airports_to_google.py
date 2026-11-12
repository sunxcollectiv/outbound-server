"""Attach a Google place to seeded airports, where the match is confident.

For each airport place (category "airport", with a code) that has no
google_place_id yet, runs one Google Text Search and links the result only when
it is confidently the same airport — see app/services/airport_linking.py for
the rule. A linked airport gets its Google id, a real address in place of the
seed's "City, Country", and cached Google data (so its cover photo works).
Airports with no confident match are left alone and listed with the reason.

If the matched Google place already belongs to another place (an airport added
through the app before the seed), the seeded airport is merged into that place:
it gets the code, anything pointing at the seeded row is repointed, and the
seeded row is deleted.

    GOOGLE_MAPS_API_KEY=... uv run python scripts/link_airports_to_google.py --dry-run --limit 25

Each airport costs one billed Text Search request, so try a small --dry-run
first and read the decisions before a full run. Resumable: linked airports are
skipped on the next run. Unmatched ones are searched again, so use --after to
carry on from where a run stopped rather than paying for them twice.
The run stops by itself after five failed searches in a row (no network, a bad
key) and prints the --after value to resume with.

Runs against DATABASE_URL (the dev database by default).
"""
from __future__ import annotations

import argparse
import time

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Place
from app.services.airport_linking import link_airport
from app.services.google_places import (
    GooglePlacesError,
    GooglePlacesNotConfigured,
    get_google_places_client,
)

# This many failed searches in a row means Google is unreachable, not that one
# airport is odd: stop instead of failing through the rest of the list.
MAX_FAILURES_IN_A_ROW = 5


def main() -> None:
    parser = argparse.ArgumentParser(description="Link seeded airports to Google places.")
    parser.add_argument("--limit", type=int, help="Process at most this many airports.")
    parser.add_argument("--after", help="Only codes after this one (resume a run), e.g. LAX.")
    parser.add_argument("--codes", nargs="+", help="Only these codes, e.g. LAX HND.")
    parser.add_argument("--sleep", type=float, default=0.1, help="Seconds between airports.")
    parser.add_argument("--dry-run", action="store_true", help="Search and decide, but don't write.")
    args = parser.parse_args()

    client = get_google_places_client()

    with SessionLocal() as session:
        query = (
            select(Place)
            .where(
                Place.category == "airport",
                Place.code.is_not(None),
                Place.google_place_id.is_(None),
            )
            .order_by(Place.code)
        )
        if args.codes:
            query = query.where(Place.code.in_([code.upper() for code in args.codes]))
        if args.after:
            query = query.where(Place.code > args.after.upper())
        if args.limit:
            query = query.limit(args.limit)
        airports = list(session.scalars(query))

        print(f"{len(airports)} unlinked airport(s) to process.")
        linked = merged = unmatched = failed = 0
        failures_in_a_row = 0
        last_ok: str | None = None

        for index, airport in enumerate(airports, start=1):
            prefix = f"  [{index}/{len(airports)}] {airport.code} {airport.name}"
            try:
                result = link_airport(session, client, airport)
            except GooglePlacesNotConfigured:
                raise SystemExit("GOOGLE_MAPS_API_KEY is not set — nothing to search.")
            except GooglePlacesError as exc:
                session.rollback()
                failed += 1
                failures_in_a_row += 1
                print(f"{prefix}: FAILED ({exc})")
                if failures_in_a_row >= MAX_FAILURES_IN_A_ROW:
                    # The network or the key is down; carrying on would only
                    # fail the rest of the list too.
                    print(
                        f"Stopping: {failures_in_a_row} searches failed in a row. "
                        f"Re-run with --after {last_ok or '<code>'} once Google is reachable."
                    )
                    break
                continue
            failures_in_a_row = 0
            last_ok = airport.code

            if result.linked:
                linked += 1
                merged += result.merged_into is not None
                verb = "would link" if args.dry_run else "linked"
                print(f"{prefix}: {verb} -> {result.google_name} ({result.reason})")
            else:
                unmatched += 1
                print(f"{prefix}: no confident match ({result.reason})")

            if args.dry_run:
                session.rollback()
            else:
                session.commit()

            if args.sleep:
                time.sleep(args.sleep)

    done = "would link" if args.dry_run else "linked"
    print(
        f"Done: {done} {linked} ({merged} by merging into an existing place), "
        f"no confident match {unmatched}, failed {failed}."
    )


if __name__ == "__main__":
    main()
