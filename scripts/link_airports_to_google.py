"""Link seeded airports to Google places, and fetch their Google details.

Two passes, both over airport places (category "airport", with a code):

1. **Details.** Airports that have a google_place_id but no Google data yet —
   what a new server's seed leaves (it seeds ids only, see
   app/seeds/_airport_google_ids.py) — get one Place Details request each. That
   caches their data (so the cover photo works) and replaces the seed's
   "City, Country" placeholder with Google's real address.
2. **Search.** Airports with no google_place_id at all get one Google Text
   Search each, and are linked only when the result is confidently the same
   airport — see app/services/airport_linking.py for the rule. Those with no
   confident match are left alone and listed with the reason. If the matched
   Google place already belongs to another place (an airport added through the
   app before the seed), the seeded airport is merged into it: it gets the
   code, anything pointing at the seeded row is repointed, and the row deleted.

    GOOGLE_MAPS_API_KEY=... uv run python scripts/link_airports_to_google.py --dry-run --limit 25

Every request is billed, so try a small --dry-run first and read the decisions
before a full run. --skip-search runs the details pass only (the usual thing on
a new server). Resumable: airports with Google data are never fetched again, and
linked airports are not searched again. Unmatched ones are, so use --after to
carry on from where a run stopped rather than paying for them twice. A pass
stops by itself after five failed requests in a row (no network, a bad key) and
prints the --after value to resume with.

Runs against DATABASE_URL (the dev database by default).
"""
from __future__ import annotations

import argparse
import time
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import Place
from app.services.airport_linking import LinkResult, fill_airport_details, link_airport
from app.services.google_places import (
    GooglePlacesClient,
    GooglePlacesError,
    GooglePlacesNotConfigured,
    get_google_places_client,
)

# This many failed requests in a row means Google is unreachable, not that one
# airport is odd: stop instead of failing through the rest of the list.
MAX_FAILURES_IN_A_ROW = 5


def _airports(session: Session, args: argparse.Namespace, *conditions) -> list[Place]:
    query = (
        select(Place)
        .where(Place.category == "airport", Place.code.is_not(None), *conditions)
        .order_by(Place.code)
    )
    if args.codes:
        query = query.where(Place.code.in_([code.upper() for code in args.codes]))
    if args.after:
        query = query.where(Place.code > args.after.upper())
    if args.limit:
        query = query.limit(args.limit)
    return list(session.scalars(query))


def _run_pass(
    session: Session,
    airports: list[Place],
    step: Callable[[Place], LinkResult],
    *,
    done_verb: str,
    miss_label: str,
    args: argparse.Namespace,
) -> dict[str, int]:
    """Run `step` over each airport, committing (or, dry, rolling back) one at a
    time, and print a line per airport. Returns counts."""
    counts = {"done": 0, "merged": 0, "missed": 0, "failed": 0}
    failures_in_a_row = 0
    last_ok: str | None = None
    verb = f"would {done_verb.removesuffix('ed')}" if args.dry_run else done_verb

    for index, airport in enumerate(airports, start=1):
        prefix = f"  [{index}/{len(airports)}] {airport.code} {airport.name}"
        try:
            result = step(airport)
        except GooglePlacesNotConfigured:
            raise SystemExit("GOOGLE_MAPS_API_KEY is not set — nothing to fetch.")
        except GooglePlacesError as exc:
            session.rollback()
            counts["failed"] += 1
            failures_in_a_row += 1
            print(f"{prefix}: FAILED ({exc})")
            if failures_in_a_row >= MAX_FAILURES_IN_A_ROW:
                # The network or the key is down; carrying on would only fail
                # the rest of the list too.
                print(
                    f"Stopping: {failures_in_a_row} requests failed in a row. "
                    f"Re-run with --after {last_ok or '<code>'} once Google is reachable."
                )
                break
            continue
        failures_in_a_row = 0
        last_ok = airport.code

        if result.linked:
            counts["done"] += 1
            counts["merged"] += result.merged_into is not None
            print(f"{prefix}: {verb} -> {result.google_name} ({result.reason})")
        else:
            counts["missed"] += 1
            print(f"{prefix}: {miss_label} ({result.reason})")

        if args.dry_run:
            session.rollback()
        else:
            session.commit()

        if args.sleep:
            time.sleep(args.sleep)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch Google details for seeded airports and link the rest."
    )
    parser.add_argument("--limit", type=int, help="Process at most this many airports per pass.")
    parser.add_argument("--after", help="Only codes after this one (resume a run), e.g. LAX.")
    parser.add_argument("--codes", nargs="+", help="Only these codes, e.g. LAX HND.")
    parser.add_argument("--sleep", type=float, default=0.1, help="Seconds between airports.")
    parser.add_argument("--skip-search", action="store_true", help="Only the details pass.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and decide, but don't write.")
    args = parser.parse_args()

    client: GooglePlacesClient = get_google_places_client()

    with SessionLocal() as session:
        to_fill = _airports(
            session, args, Place.google_place_id.is_not(None), Place.google_data.is_(None)
        )
        print(f"Details: {len(to_fill)} airport(s) with a Google id and no Google data.")
        filled = _run_pass(
            session,
            to_fill,
            lambda airport: fill_airport_details(client, airport),
            done_verb="filled",
            miss_label="NOT FILLED",
            args=args,
        )
        print(
            f"Details done: {'would fill' if args.dry_run else 'filled'} {filled['done']}, "
            f"unknown to Google {filled['missed']}, failed {filled['failed']}."
        )

        if args.skip_search:
            return

        to_link = _airports(session, args, Place.google_place_id.is_(None))
        print(f"Search: {len(to_link)} unlinked airport(s).")
        searched = _run_pass(
            session,
            to_link,
            lambda airport: link_airport(session, client, airport),
            done_verb="linked",
            miss_label="no confident match",
            args=args,
        )
        print(
            f"Search done: {'would link' if args.dry_run else 'linked'} {searched['done']} "
            f"({searched['merged']} by merging into an existing place), "
            f"no confident match {searched['missed']}, failed {searched['failed']}."
        )


if __name__ == "__main__":
    main()
