"""Grant (or with --revoke, take away) admin on an account.

Admins can manage announcements (/announcements). No endpoint hands this out, so it's
done from here:

    uv run python scripts/make_admin.py someone@example.com
    uv run python scripts/make_admin.py someone@example.com --revoke
"""
from __future__ import annotations

import argparse

from sqlalchemy import select

from app.db import SessionLocal
from app.models import User


def main() -> None:
    parser = argparse.ArgumentParser(description="Grant or revoke admin on an account.")
    parser.add_argument("email", help="The account's email address.")
    parser.add_argument("--revoke", action="store_true", help="Take admin away instead.")
    args = parser.parse_args()

    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.email == args.email.lower()))
        if user is None:
            raise SystemExit(f"No account with email {args.email}")
        user.is_admin = not args.revoke
        session.commit()
        print(f"{user.email} is {'now' if user.is_admin else 'no longer'} an admin.")


if __name__ == "__main__":
    main()
