"""Mint a service (machine-to-machine) token — a bearer credential not tied to
any user — for a trusted client like the admin console (wravel-manage) to call
the /admin APIs. The raw token is printed once; only its SHA-256 is stored.

    uv run python scripts/issue_service_token.py "wravel-manage console"
    uv run python scripts/issue_service_token.py "ci" --days 30

The console sends it as `Authorization: Bearer <token>`; its Vite dev proxy
forwards /api/* to the backend, so no CORS is involved. Runs against
DATABASE_URL (the dev DB by default), NOT the test database.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta

from app.db import SessionLocal
from app.models import ServiceToken
from app.security import hash_token, new_token


def main() -> None:
    parser = argparse.ArgumentParser(description="Mint an admin service (M2M) token.")
    parser.add_argument("name", help="A label for the token, e.g. 'wravel-manage console'.")
    parser.add_argument(
        "--days", type=int, default=None, help="Expiry in days (default: never expires)."
    )
    args = parser.parse_args()

    token = new_token()
    expires_at = datetime.now(UTC) + timedelta(days=args.days) if args.days else None
    with SessionLocal() as session:
        session.add(
            ServiceToken(name=args.name, token_hash=hash_token(token), expires_at=expires_at)
        )
        session.commit()

    when = "never expires" if expires_at is None else f"expires {expires_at:%Y-%m-%d}"
    print(f"Service token '{args.name}' created ({when}).")
    print()
    print("Paste into wravel-manage/riyadh/.env.local and restart `npm run dev`:")
    print(f"VITE_API_TOKEN={token}")


if __name__ == "__main__":
    main()
