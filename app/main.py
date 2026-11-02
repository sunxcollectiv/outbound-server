from fastapi import Depends, FastAPI

from app.api import (
    admin,
    auth,
    areas,
    notifications,
    places,
    received_images,
    trip_items,
    trips,
    users,
)
from app.api.deps import get_admin_principal

DESCRIPTION = """
Backend for Outbound, a collaborative trip-itinerary app.

Authenticate at `/auth`, then send the returned token as
`Authorization: Bearer <token>` on every other call. The **[admin]** APIs under
`/admin/*` require a service (machine-to-machine) token — minted with
`scripts/issue_service_token.py`; an ordinary user token gets 403 there. There
are no human admins.
"""

# Section descriptions shown in the Swagger UI. Tag names must match the
# `tags=[...]` each router declares.
TAGS_METADATA = [
    {"name": "auth", "description": "Register, log in, and log out. Issues an opaque bearer token to send as `Authorization: Bearer <token>`."},
    {"name": "users", "description": "The signed-in account, plus looking up other users by id or email."},
    {"name": "trips", "description": "Trips, their per-date day scaffolding, and membership (people on a trip, keyed by email)."},
    {"name": "trip items", "description": "Everything planned on a trip — lodging, transit (with legs), activities — plus participants and RSVPs."},
    {"name": "places", "description": "Google-backed place records and their cached Place Details."},
    {"name": "areas", "description": "The country / territory / city taxonomy trips are tagged with, and their cover photos."},
    {"name": "received-mail", "description": "[admin] Inbound travel-confirmation emails (via Resend), as a retrieve → extract → import pipeline. Every route is admin-only."},
    {"name": "received-images", "description": "[admin] Uploaded booking screenshots, as an upload → extract → import pipeline. Every route is admin-only."},
    {"name": "notifications", "description": "The in-app inbox, per-category/channel preferences, and push-device registration."},
    {"name": "announcements", "description": "[admin] Broadcast messages that fan out into every user's inbox. Every route is admin-only."},
    {"name": "admin", "description": "[admin] Management/dashboard APIs (e.g. browse all users). Every route is admin-only."},
    {"name": "meta", "description": "Service health and metadata."},
]

app = FastAPI(
    title="Outbound API",
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
