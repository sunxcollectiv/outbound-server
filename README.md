# Wravel API

Backend for Wravel, a collaborative trip-itinerary app. FastAPI, SQLAlchemy and
Postgres 17, with Alembic migrations. Interactive API docs are served at `/docs`.

## Requirements

- Python 3.12 or newer, managed with [uv](https://docs.astral.sh/uv/)
- Postgres 17 (Docker Compose runs one for local development)

## Environments

There are three: `local` (your machine), `staging` and `prod` (a server each).
Each reads its settings from a `.env` in the repo root, and every `.env` is
written by the same script, so they share one layout and differ only in values:

```bash
deploy/newenv.sh <local|staging|prod>
```

It generates the Postgres password, sets the per-environment switches, and asks
for each secret in turn (Enter leaves it blank and that feature off):

| | local | staging | prod |
|---|---|---|---|
| API runs | from the uv venv | in Compose, behind nginx | in Compose, behind nginx |
| Postgres | Compose, `127.0.0.1:4930` | same | same |
| Database, user | `wravel`, `wravel` | same | same |
| `APNS_USE_SANDBOX` | `true` | `true` (Xcode builds) | `false` |
| `STOREKIT_ALLOW_XCODE` | `true` | `false` | `false` |
| `STOREKIT_APP_APPLE_ID` | none | none | asked |

It refuses to replace an existing `.env`: a new `POSTGRES_PASSWORD` would not
match the existing database volume, and the symptom (auth failures against your
own database) looks nothing like the cause. Pass `--force` to rewrite it
anyway: the old file is copied to `.env.bak.<timestamp>` first, and its values,
the password included, are the defaults. So to change one key, run it with
`--force` and press Enter through the rest. Prefer that to editing `.env` by
hand, so the environments stay alike. A value already exported in the shell is
used without asking, so it can also run unattended
(`GOOGLE_MAPS_API_KEY=... deploy/newenv.sh prod </dev/null`). For pushes it
asks for the APNs `.p8` file and inlines the key into `.env`.

## Local development

```bash
uv sync --extra dev                 # create .venv and install dependencies
deploy/newenv.sh local              # write .env (every key but the database can stay blank)
docker compose up -d db             # Postgres on 127.0.0.1:4930
uv run python scripts/bootstrap.py  # migrate, then seed countries, cities, regions, airports
uv run python main.py               # API on http://127.0.0.1:4830, with reload
```

## Deploying to a server

One Linux host per environment: nginx on the host terminating TLS on 443 and
proxying to loopback, and the API and Postgres as two containers from the same
`docker-compose.yml` used locally. The `app` service sits behind the `server`
Compose profile, switched on by the server's `.env` (`COMPOSE_PROFILES=server`)
and invisible locally, where the API runs from the venv. Everything the server
needs beyond that file is under `deploy/`.

| | Host port (loopback only) |
|---|---|
| API | `127.0.0.1:4830` |
| Postgres | `127.0.0.1:4930` |

These do not collide with otp-relay-server (4820 and 4920), so the two can share
a host.

### First time

The host needs Docker with the compose plugin, nginx, certbot and git. On a
small host (512 MB to 1 GB) also add a 1 GB swapfile, or the first build or
memory spike gets the API killed:

```bash
sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

Copy the APNs `.p8` key to the server if pushes are wanted
(`scp AuthKey_XXXX.p8 <server>:`), then:

```bash
git clone https://github.com/sunxcollectiv/wravel-server.git /srv/apps/wravel-server
cd /srv/apps/wravel-server
deploy/newenv.sh prod          # or staging; writes .env, mode 600
rm ~/AuthKey_*.p8              # newenv.sh inlined it into .env
deploy/deploy.sh               # builds, migrates and seeds, starts both containers
curl -s http://127.0.0.1:4830/health     # {"status":"ok"}
```

`deploy.sh` runs `scripts/bootstrap.py`, so the first deploy also seeds about
1,700 areas and 4,100 airports. To start from existing data instead, restore it
before the first `deploy.sh` (see [Moving data between
environments](#moving-data-between-environments)).

Then put nginx in front, as under [nginx](#nginx), and get the certificate:

```bash
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d <domain>
```

Then the one-offs. Scripts run in a one-off container, against the same
database:

1. **Mint a service token** for the `/admin` APIs (the admin console). It is
   printed once; only its hash is stored.

   ```bash
   docker compose run --rm app python scripts/issue_service_token.py "admin console"
   ```
2. **Link the airports to Google** (needs `GOOGLE_MAPS_API_KEY`; one billed
   Place Details request per airport, about 4,100). See [Airports](#airports).

   ```bash
   docker compose run --rm app python scripts/link_airports_to_google.py --skip-search
   ```
3. **Backfill area photos** (optional; needs `UNSPLASH_ACCESS_KEY`). Mind the
   rate limit: 50 requests an hour on a demo key. The script is resumable.

   ```bash
   docker compose run --rm app python scripts/backfill_area_photos.py --kind country
   ```

   Without a backfill, an area still gets photos on demand: when a trip tagged
   with it is opened, the app calls `POST /areas/refresh-photos`. That also
   needs the Unsplash key, and returns 503 without one.
4. **Point the integrations at it.** In the Resend dashboard, add a webhook for
   `email.received` to `https://<domain>/webhooks/resend`, put its signing
   secret in `.env` (`deploy/newenv.sh prod --force`) and run `deploy/deploy.sh`.
   Point the iOS and Android builds and wravel-manage at `https://<domain>`.

### Deploying a new version

```bash
/srv/apps/wravel-server/deploy/deploy.sh
```

Fast-forwards the checkout to `origin/main`, rebuilds the image, runs
`bootstrap.py` with the new image (new migrations and seed additions; it never
edits or removes rows), then recreates the API container. It is idempotent, and
re-running it after a failure is always the right move. Postgres is not
restarted by a deploy. Nothing in it assumes a human is watching, so a GitHub
Actions job can later run it over ssh unchanged.

### The server's `.env`

`deploy/newenv.sh` writes it, mode 600 from the first byte. It differs from a
local one in three ways:

- `COMPOSE_PROFILES=server`, which is what makes the `app` service exist.
- No `DATABASE_URL`. The compose file builds the container's from `POSTGRES_*`,
  so there is no second string to keep in agreement.
- `PORT`, the host-side loopback port nginx proxies to. Inside the container
  the API always listens on 4830.

### nginx

The site file lives on the server, not in this repo, since it carries the real
domain and certbot rewrites it in place. Write it as
`/etc/nginx/sites-available/wravel`, symlink it into `sites-enabled/`, then run
certbot as above, which adds the 443 listener and the redirect:

```nginx
server {
    listen 80;
    server_name <domain>;

    # Screenshots are uploaded up to 10 MB (MAX_IMAGE_BYTES); nginx's default is 1 MB.
    client_max_body_size 12m;

    location / {
        proxy_pass http://127.0.0.1:4830;
        proxy_set_header Host $host;
        # Set, not appended: uvicorn trusts this header from anywhere, which is
        # safe only because nginx overwrites it with the real client address.
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### Postgres and the firewall

`docker-compose.yml` publishes both containers on `127.0.0.1` only. **Do not
widen that**: Compose's short port syntax defaults to all interfaces, and a
droplet has no firewall in front of it by default. Belt and braces:

```bash
ufw default deny incoming
ufw allow 22,80,443/tcp
ufw enable
```

To reach the database from your machine (a GUI client, `pg_dump`), tunnel to
it: `ssh -L 4931:127.0.0.1:4930 <server>`, then connect to `127.0.0.1:4931` as
`wravel` with the server's `POSTGRES_PASSWORD`.

### Day to day

```bash
docker compose ps                           # what is running, and its health
docker compose logs -f app                  # API logs
docker compose run --rm app python scripts/<script>.py
docker compose exec db psql -U wravel       # a SQL shell
```

There is no systemd unit: `restart: unless-stopped` brings both containers back
after a crash or reboot, as long as Docker is enabled at boot
(`systemctl is-enabled docker`).

### Moving data between environments

Users, trips, credits and notifications are not seeded. To copy a database (for
example local to a new prod), dump it from the source and restore it into the
target's empty database:

```bash
# on the source, from the repo root
docker compose exec -T db pg_dump -U wravel -Fc wravel > wravel.dump
scp wravel.dump <server>:/srv/apps/wravel-server/

# on the target, before its first deploy/deploy.sh
docker compose up -d --wait db
docker compose exec -T db pg_restore -U wravel -d wravel --no-owner < wravel.dump
deploy/deploy.sh
```

`deploy.sh` then migrates the restored schema to head. Delete `wravel.dump`
from both machines afterwards: it holds every user's data.

### Backups

Nothing backs the database up yet. A `pg_dump` like the one above on a timer,
copied off the server, is enough. Until then, take one before risky deploys.

### What is and is not seeded

| Data | Source |
|---|---|
| Countries, territories, cities, islands, regions | `scripts/bootstrap.py` (lists in `app/seeds/`) |
| Pricing | In code (`app/services/pricing.py`); nothing to seed |
| Area photos | Unsplash, via the backfill script or on demand when a trip is opened |
| Airports | `scripts/bootstrap.py` (list in `app/seeds/_airports_data.py`), as places with category `airport` |
| Other places | Not seeded. Created from Google as people add them to trips |
| Users, trips, credits, notifications | Not seeded. Move them with `pg_dump` / `pg_restore` |

A few areas with no `kind` (for example "New York") appear on their own: they
are derived from a place's address when that place is first added.

## Configuration

Settings are read from the environment or `.env` (`app/config.py`), which
`deploy/newenv.sh` writes (see [Environments](#environments)). Everything
except the database is optional; a feature with no key is switched off.

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Postgres connection. Defaults to the Compose database |
| `GOOGLE_MAPS_API_KEY` | Place search and Place Details |
| `UNSPLASH_ACCESS_KEY` | Fetching area cover photos |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_WORKSPACE_ID` | Parsing forwarded emails and screenshots into trip items |
| `RESEND_API_KEY` | Retrieving forwarded travel-confirmation emails |
| `RESEND_WEBHOOK_SECRET` | Accepting Resend's `email.received` webhook at `POST /webhooks/resend` (the webhook's signing secret, `whsec_...`). Without it the route answers 503 |
| `APNS_KEY_ID`, `APNS_TEAM_ID`, and `APNS_PRIVATE_KEY` or `APNS_KEY_PATH` | Push notifications. Without a key, pushes are skipped |
| `APNS_USE_SANDBOX` | `true` (default) for Xcode builds; `false` for TestFlight and App Store builds |
| `STOREKIT_APP_APPLE_ID` | Required to accept production in-app purchases |
| `STOREKIT_ALLOW_XCODE` | Accept unsigned Xcode purchases. Local servers only |

Never commit `.env` or an APNs `.p8` key; both are git-ignored.

## Forwarded email

A travel confirmation forwarded to the Resend inbound address becomes trip items without anyone asking for it:

1. Resend posts an `email.received` event to `POST /webhooks/resend`. The call is authenticated by its signature, checked against `RESEND_WEBHOOK_SECRET`.
2. The email is stored as a receipt and the webhook answers.
3. In the background the email is checked against its sender, read by Claude, and imported into the sender's trip.

An email is only imported when its From address is a registered user's email and the booking's dates overlap exactly one of their trips. Otherwise the receipt is kept with the reason (`ignored`, `rejected`, or `extracted` with no trip chosen).

To set it up, add a webhook in the Resend dashboard for the `email.received` event pointing at `https://<host>/webhooks/resend`, and put its signing secret in `RESEND_WEBHOOK_SECRET`.

`POST /admin/received/mail/process` runs the same pipeline by pulling recent mail from Resend. Use it to catch up on anything a webhook missed, such as mail that arrived while the server was down.

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

## Airports

Airports are places with `category` set to `airport` and their IATA code in
`code`. The seeded list is every airport with an IATA code and scheduled
commercial service, from [OurAirports](https://ourairports.com/data/) (public
domain).

`GET /places/airports?q=…&limit=…` searches them by code and name for a
manually entered flight. Any signed-in user can call it. The order is: the
exact code, then codes starting with `q`, names starting with `q`, names with a
word starting with `q`, and names containing `q`.

- `app/seeds/_airports_data.py`: **generated**. Do not edit it by hand.
- `app/seeds/_airport_google_ids.py`: **generated** by
  `scripts/export_airport_google_ids.py`. Do not edit it by hand.
  Regenerate from the current OurAirports file:

  ```bash
  uv run --with geonamescache --with timezonefinder python scripts/generate_airports.py
  ```

The seed only adds airports whose code is not in the database yet. It never
renames, moves or removes an existing one, so an airport that closes or changes
code upstream has to be fixed by hand through `PATCH /admin/places/{id}`.

Each seeded airport also gets its Google place id from
`app/seeds/_airport_google_ids.py`, matched and reviewed once and exported from
the database where that happened. Only the ids are seeded: Google's terms allow
storing a place id indefinitely but not its address or photos, so a new server
fetches those itself (needs `GOOGLE_MAPS_API_KEY`; one billed Place Details
request per airport):

```bash
uv run python scripts/link_airports_to_google.py --skip-search
```

Until then an airport shows a "City, Country" placeholder address, which the
first Google fetch replaces.

Without `--skip-search` the script also searches Google for airports that have
no id at all, and links one only when a single airport-typed result sits within
5 km of it and its name agrees (`app/services/airport_linking.py`). After
reviewing new matches, regenerate the id list from that database:

```bash
uv run python scripts/export_airport_google_ids.py
```

## Tests

```bash
uv run pytest
```

The suite needs the database server running. It drops and recreates a database
named `wravel_test` on the server `DATABASE_URL` points to, and builds it with
the real migrations. It does not touch the main database, but do not run it with
`DATABASE_URL` pointed at a production server.
