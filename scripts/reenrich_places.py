"""Re-enrich places from Google Place Details.

Walks every place linked to a Google place and refreshes its cached
`google_data` (and fills in any still-empty name/address/area/etc.), exactly
like POST /places/{id}/refresh-google but in bulk. Use it to enrich bare stubs,
or to re-pull after the Google field mask changed — e.g. adding `photos`, which
cover_photo needs (a cache fetched before that change has no photos key).

Each refresh is one billed Google call and writes to the database, so it needs
GOOGLE_MAPS_API_KEY set and commits per place. --dry-run makes no calls or writes.

    uv run python scripts/reenrich_places.py            # refresh all linked places
    uv run python scripts/reenrich_places.py --missing-photos-only
    uv run python scripts/reenrich_places.py --dry-run
    uv run python scripts/reenrich_places.py --limit 5
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.models import Place
from app.services.google_places import (
    GooglePlaceNotFound,
    GooglePlacesError,
    apply_area_from_google,
    apply_google_place,
    get_google_places_client,
)


def _needs_photos(place: Place) -> bool:
    """True when the cache is missing or predates the photos field mask."""
    return not place.google_data or "photos" not in place.google_data


def _targets(session: Session, *, missing_photos_only: bool, limit: int | None) -> list[Place]:
    query = select(Place).where(Place.google_place_id.is_not(None)).order_by(Place.created_at)
    places = list(session.scalars(query))
    if missing_photos_only:
        places = [p for p in places if _needs_photos(p)]
    if limit is not None:
        places = places[:limit]
    return places


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--missing-photos-only",
        action="store_true",
        help="only refresh places whose cache has no photos (e.g. after the mask change)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list what would be refreshed; make no Google calls and no writes",
    )
    parser.add_argument("--limit", type=int, default=None, help="refresh at most N places")
    args = parser.parse_args(argv)

    if not args.dry_run and not get_settings().google_maps_api_key:
        print("GOOGLE_MAPS_API_KEY is not set; refuse to run (use --dry-run to preview).")
        return 1

    client = get_google_places_client()

    with SessionLocal() as session:
        targets = _targets(
            session, missing_photos_only=args.missing_photos_only, limit=args.limit
        )
        print(f"{len(targets)} place(s) to refresh" + (" [dry run]" if args.dry_run else ""))

        refreshed = skipped = failed = 0
        for place in targets:
            label = place.name or place.google_place_id
            if args.dry_run:
                print(f"  would refresh: {label}")
                continue
            try:
                details = client.fetch_place(place.google_place_id)
            except GooglePlaceNotFound:
                print(f"  skip (google place not found): {label}")
                skipped += 1
                continue
            except GooglePlacesError as exc:
                print(f"  fail: {label}: {exc}")
                failed += 1
                continue
            apply_google_place(place, details)
            apply_area_from_google(session, place, details)
            session.commit()  # per place, so a later failure doesn't lose progress
            refreshed += 1
            print(f"  refreshed: {place.name or label}")

        if not args.dry_run:
            print(f"done: {refreshed} refreshed, {skipped} skipped, {failed} failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
