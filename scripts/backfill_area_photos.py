"""Backfill curated photos for areas from Unsplash.

Fetches up to a few photos per area and stores them (hotlinked — we keep the
Unsplash CDN URL, not the bytes) in the area_photos table, setting each area's
default_photo_url to the first one. Idempotent and resumable: areas that already
have photos are skipped, so you can stop and re-run.

    UNSPLASH_ACCESS_KEY=... uv run python scripts/backfill_area_photos.py --kind city --limit 50

Mind Unsplash's rate limits (demo key: 50 requests/hour; production: 5000/hour).
--sleep throttles between areas; each area costs one search request (plus one
cheap download ping per photo used).
"""
from __future__ import annotations

import argparse
import time

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Area
from app.services.unsplash import (
    UnsplashError,
    UnsplashNotConfigured,
    apply_area_photos,
    get_unsplash_client,
)


def _query_for(area: Area) -> str:
    """A search query for an area. Cities get their country for context
    ("Kyoto Japan") so we don't match a same-named place elsewhere."""
    if area.kind == "city" and area.parent is not None:
        return f"{area.name} {area.parent.name}"
    return area.name


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill area photos from Unsplash.")
    parser.add_argument("--kind", help="Only areas of this kind (e.g. city, country).")
    parser.add_argument("--limit", type=int, help="Process at most this many areas.")
    parser.add_argument("--count", type=int, default=3, help="Photos per area (default 3).")
    parser.add_argument("--sleep", type=float, default=1.0, help="Seconds between areas.")
    parser.add_argument("--dry-run", action="store_true", help="Search but don't write.")
    args = parser.parse_args()

    client = get_unsplash_client()

    with SessionLocal() as session:
        # Only areas that don't have any photos yet — keeps the run resumable.
        query = select(Area).where(~Area.photos.any()).order_by(Area.name)
        if args.kind:
            query = query.where(Area.kind == args.kind)
        if args.limit:
            query = query.limit(args.limit)
        areas = list(session.scalars(query))

        print(f"{len(areas)} area(s) without photos to process.")
        filled = skipped = failed = 0

        for index, area in enumerate(areas, start=1):
            search = _query_for(area)
            try:
                photos = client.search(search, count=args.count)
            except UnsplashNotConfigured:
                raise SystemExit("UNSPLASH_ACCESS_KEY is not set — nothing to fetch.")
            except UnsplashError as exc:
                failed += 1
                print(f"  [{index}/{len(areas)}] {area.name}: FAILED ({exc})")
                continue

            if not photos:
                skipped += 1
                print(f"  [{index}/{len(areas)}] {area.name}: no results for {search!r}")
            elif args.dry_run:
                print(f"  [{index}/{len(areas)}] {area.name}: would store {len(photos)} photo(s)")
            else:
                apply_area_photos(session, area, photos)
                for photo in photos:
                    client.trigger_download(photo.download_location)
                session.commit()
                filled += 1
                print(f"  [{index}/{len(areas)}] {area.name}: stored {len(photos)} photo(s)")

            if args.sleep:
                time.sleep(args.sleep)

    print(f"Done: filled {filled}, no-results {skipped}, failed {failed}.")


if __name__ == "__main__":
    main()
