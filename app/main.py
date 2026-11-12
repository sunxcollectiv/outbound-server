from fastapi import Depends, FastAPI

from app.api import (
    admin,
    auth,
    credits,
    areas,
    notifications,
    places,
    pricing,
    received_images,
    trip_items,
    trips,
    users,
    webhooks,
)
from app.api.deps import get_admin_principal

DESCRIPTION = """
Backend for Wravel, a collaborative trip-itinerary app.

Authenticate at `/auth`, then send the returned token as
`Authorization: Bearer <token>` on every other call. The **[admin]** APIs under
`/admin/*` require a service (machine-to-machine) token — minted with
`scripts/issue_service_token.py`; an ordinary user token gets 403 there. There
are no human admins.
"""

# Section descriptions shown in the Swagger UI. Tag names must match the
# `tags=[...]` each router declares.
# User-facing sections first, then the admin-only sections, each its own
# `admin/<area>` group so the privileged surface is cleanly divided in the docs.
TAGS_METADATA = [
    {"name": "auth", "description": "Register, log in, and log out. Issues an opaque bearer token to send as `Authorization: Bearer <token>`."},
    {"name": "users", "description": "The signed-in account, plus looking up other users by id or email."},
    {"name": "trips", "description": "Trips, their per-date day scaffolding, and membership (people on a trip, keyed by email)."},
    {"name": "trip items", "description": "Everything planned on a trip — lodging, transit (with legs), activities — plus participants and RSVPs."},
    {"name": "places", "description": "Google-backed place records and their cached Place Details."},
    {"name": "areas", "description": "The country / territory / city taxonomy trips are tagged with, and their cover photos."},
    {"name": "received-images", "description": "Importing a booking screenshot into your own trips in one call (POST /received/images/process)."},
    {"name": "notifications", "description": "The in-app inbox, per-category/channel preferences, and push-device registration."},
    {"name": "pricing", "description": "The rate tables trips are priced from."},
    {"name": "credits", "description": "The caller's credit balance and ledger; trips are paid for in credits."},
    {"name": "webhooks", "description": "Calls from outside services, authenticated by their signature rather than a bearer token: Resend announcing a received email."},
    {"name": "meta", "description": "Service health and metadata."},
    # --- admin (service-token only); one section per area ---
    {"name": "admin/users", "description": "[admin] Browse every user account (paginated)."},
    {"name": "admin/trips", "description": "[admin] Browse every trip (paginated), with owner and member count."},
    {"name": "admin/notifications", "description": "[admin] Send a notification to specific people by id or email."},
    {"name": "admin/credits", "description": "[admin] Grant credits to a person and read their ledger."},
    {"name": "admin/announcements", "description": "[admin] Broadcast messages that fan out into every user's inbox."},
    {"name": "admin/areas", "description": "[admin] Curate the area taxonomy — create, edit, delete."},
    {"name": "admin/places", "description": "[admin] Manage the shared place catalogue — browse, edit, force a Google refresh."},
    {"name": "admin/received-mail", "description": "[admin] Inbound travel-confirmation mail pipeline (dev tooling): retrieve → extract → import."},
    {"name": "admin/received-images", "description": "[admin] Image pipeline step endpoints (dev tooling): upload → extract → import."},
]

app = FastAPI(
    title="Wravel API",
    version="0.1.0",
    description=DESCRIPTION,
    openapi_tags=TAGS_METADATA,
)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(trips.router)
app.include_router(trip_items.router)
app.include_router(places.router)
app.include_router(areas.router)
app.include_router(received_images.router)  # user-facing POST /received/images/process
app.include_router(notifications.router)
app.include_router(pricing.router)
app.include_router(credits.router)
app.include_router(webhooks.router)  # signature-checked, no bearer token
# Everything privileged lives under /admin/* (management endpoints + the
# announcements and received-mail/images routers, included inside admin.router).
# The admin gate is applied once, here, so no route under /admin can be
# non-admin by construction. It accepts an admin user's token or a service
# (M2M) token.
app.include_router(admin.router, dependencies=[Depends(get_admin_principal)])


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    """Liveness probe. Always returns `{"status": "ok"}` when the app is up."""
    return {"status": "ok"}
