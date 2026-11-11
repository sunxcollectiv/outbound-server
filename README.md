# Outbound API

Backend for Outbound, a collaborative trip-itinerary app. FastAPI, SQLAlchemy and
Postgres 17, with Alembic migrations. Interactive API docs are served at `/docs`.

## Requirements

- Python 3.12 or newer, managed with [uv](https://docs.astral.sh/uv/)
- Postgres 17 (Docker Compose runs one for local development)

## Local development

```bash
uv sync --extra dev               # create .venv and install dependencies
docker compose up -d db           # Postgres on 127.0.0.1:4930
uv run python scripts/bootstrap.py  # migrate, then seed countries, cities, regions
uv run python main.py             # API on http://127.0.0.1:4830, with reload
```

With no `.env`, the app connects to the Compose database and the optional
integrations below stay off.

## Setting up a fresh server

1. **Configure.** Set `DATABASE_URL` and the secrets you need (see
   [Configuration](#configuration)), as environment variables or in a `.env`
   file in the working directory.
2. **Bootstrap the database.**

   ```bash
   uv run python scripts/bootstrap.py
   ```

   This runs the migrations to head, then seeds countries and territories,
   cities, and islands and regions: about 1,700 areas. Every step is idempotent,
   so run it again after each deploy to apply new migrations and pick up
   additions to the seed lists. It never edits or removes existing rows. Pass
   `--skip-migrations` if your deploy pipeline already runs Alembic.
3. **Mint a service token** for the `/admin` APIs. It is printed once; only its
   hash is stored.

   ```bash
   uv run python scripts/issue_service_token.py "admin console"
   ```
4. **Backfill area photos** (optional; needs `UNSPLASH_ACCESS_KEY`). Mind the
   rate limit: 50 requests an hour on a demo key. The script is resumable.

   ```bash
   uv run python scripts/backfill_area_photos.py --kind country
   ```

   Without a backfill, an area still gets photos on demand: when a trip tagged
   with it is opened, the app calls `POST /areas/refresh-photos`. That also
   needs the Unsplash key, and returns 503 without one.
5. **Run the server.** `main.py` binds to localhost with reload on, which is for
   development only. On a server, run uvicorn directly:

   ```bash
   uv run uvicorn app.main:app --host 0.0.0.0 --port 4830
   ```

### What is and is not seeded

| Data | Source |
|---|---|
| Countries, territories, cities, islands, regions | `scripts/bootstrap.py` (lists in `app/seeds/`) |
| Pricing | In code (`app/services/pricing.py`); nothing to seed |
| Area photos | Unsplash, via the backfill script or on demand when a trip is opened |
| Places | Not seeded. Created from Google as people add them to trips |
| Users, trips, credits, notifications | Not seeded. Move them with `pg_dump` / `pg_restore` |

A few areas with no `kind` (for example "New York") appear on their own: they
are derived from a place's address when that place is first added.

## Configuration

Settings are read from the environment or `.env` (`app/config.py`). Everything
except the database is optional; a feature with no key is switched off.

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Postgres connection. Defaults to the Compose database |
| `GOOGLE_MAPS_API_KEY` | Place search and Place Details |
| `UNSPLASH_ACCESS_KEY` | Fetching area cover photos |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_WORKSPACE_ID` | Parsing forwarded emails and screenshots into trip items |
| `RESEND_API_KEY` | Retrieving forwarded travel-confirmation emails |
| `APNS_KEY_ID`, `APNS_TEAM_ID`, and `APNS_PRIVATE_KEY` or `APNS_KEY_PATH` | Push notifications. Without a key, pushes are skipped |
| `APNS_USE_SANDBOX` | `true` (default) for Xcode builds; `false` for TestFlight and App Store builds |
| `STOREKIT_APP_APPLE_ID` | Required to accept production in-app purchases |
| `STOREKIT_ALLOW_XCODE` | Accept unsigned Xcode purchases. Local servers only |

Never commit `.env` or an APNs `.p8` key; both are git-ignored.

## Areas

Areas are the countries, cities, islands and regions a trip is tagged with.

- `app/seeds/countries.py`: countries and territories, hand-written.
- `app/seeds/_cities_data.py`: cities, **generated**. Do not edit it by hand.
  Edit the curated lists in `scripts/generate_cities.py` and regenerate:

  ```bash
  uv run --with geonamescache python scripts/generate_cities.py
  ```

  New cities belong in `CURATED_TRAVEL`, which is chosen by travel demand and
  appended after the original 1,000, so existing entries never change.
- `app/seeds/_regions_data.py`: islands and regions, hand-written.

Removing an entry from a list does not delete its row. Delete it through
`DELETE /admin/areas/{id}`.

## Tests

```bash
uv run pytest
```

The suite needs the database server running. It drops and recreates a database
named `outbound_test` on the server `DATABASE_URL` points to, and builds it with
the real migrations. It does not touch the main database, but do not run it with
`DATABASE_URL` pointed at a production server.
