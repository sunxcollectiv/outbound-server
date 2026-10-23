import uuid
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from app.models import (
    EmailReceiptStatus,
    RsvpStatus,
    TransitMode,
    TripItemKind,
    TripItemRequirement,
    TripItemScope,
    TripItemVisibility,
    TripRole,
)
from app.services.timekeeping import is_valid_timezone


def _valid_timezone(value: str) -> str:
    if not is_valid_timezone(value):
        raise ValueError(f"unknown IANA timezone: {value!r}")
    return value


TimezoneName = Annotated[str, AfterValidator(_valid_timezone)]
Email = Annotated[EmailStr, AfterValidator(str.lower)]
Url = Annotated[str, Field(max_length=2048)]
Tag = Annotated[str, Field(min_length=1, max_length=60)]


class ReadModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- users ---


class UserCreate(BaseModel):
    email: Email
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=120)
    avatar_url: Url | None = None


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    avatar_url: Url | None = None


class UserRead(ReadModel):
    id: uuid.UUID
    email: str
    display_name: str
    avatar_url: str | None
    created_at: datetime


# --- auth ---


class Credentials(BaseModel):
    email: Email
    # No minimum here: a too-short guess is a failed login, not a 422.
    password: str = Field(max_length=128)


class TokenRead(BaseModel):
    """`token` goes in the `Authorization: Bearer` header of later requests."""

    token: str
    expires_at: datetime
    user: UserRead


# --- areas (the place-on-the-map a place sits in) ---


class AreaBase(BaseModel):
    kind: str | None = Field(default=None, max_length=60)
    level: int | None = None
    parent_id: uuid.UUID | None = None
    # The cover image and the attribution that must be shown with it. Denormalized
    # onto the area so ordinary reads never touch area_photos. Settable by hand.
    default_photo_url: Url | None = None
    default_photo_attribution: str | None = Field(default=None, max_length=500)
    default_photo_attribution_url: Url | None = None


class AreaCreate(AreaBase):
    name: str = Field(min_length=1, max_length=200)


class AreaUpdate(AreaBase):
    name: str | None = Field(default=None, min_length=1, max_length=200)


class AreaRef(ReadModel):
    """A compact reference to an area, e.g. the country a city sits in, enough
    to label a search result ("Paris · France") without a second request."""

    id: uuid.UUID
    name: str
    kind: str | None


class AreaPhotoRead(ReadModel):
    """A curated area photo. `url` is a hotlink to the provider's CDN (Unsplash);
    `attribution`/`attribution_url` must be shown with the image."""

    id: uuid.UUID
    url: str
    source: str
    attribution: str | None
    attribution_url: str | None
    position: int
    width: int | None
    height: int | None
    color: str | None


class AreaRead(ReadModel):
    id: uuid.UUID
    name: str
    kind: str | None
    level: int | None
    parent_id: uuid.UUID | None
    parent: AreaRef | None = None
    # Just the cover (from the area row); the candidate photos are not loaded on
    # ordinary reads — see AreaDetailRead for those.
    default_photo_url: str | None
    default_photo_attribution: str | None
    default_photo_attribution_url: str | None
    created_at: datetime
    updated_at: datetime


class AreaDetailRead(AreaRead):
    """A single area plus its candidate photos — the admin/detail view. Loading
    `photos` here keeps area_photos off the hot read paths (trip lists, search)."""

    photos: list[AreaPhotoRead] = []


class AreaPhotosRefreshRequest(BaseModel):
    """The area ids a trip flagged as missing photos (its areas_missing_photos)."""

    area_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)


class AreaPhotosRefreshResponse(BaseModel):
    """Current state of the requested areas, keyed by id, after a best-effort
    fetch — the client patches its cover images by id. Ids that don't match an
    area are omitted."""

    areas: dict[uuid.UUID, AreaRead]


# --- places (specific spots in our catalogue) ---


class PlaceBase(BaseModel):
    category: str | None = Field(default=None, max_length=60)
    notes: str | None = None
    photo_url: Url | None = None
    address: str | None = Field(default=None, max_length=500)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    timezone: TimezoneName | None = None


class PlaceUpdate(PlaceBase):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    area_id: uuid.UUID | None = None
    google_place_id: str | None = Field(default=None, min_length=1, max_length=255)


class PlaceCreate(BaseModel):
    """The one thing a client supplies to register a place: a Google place id.
    Everything else is filled in later (enrichment, then review)."""

    google_place_id: str = Field(min_length=1, max_length=255)


class PhotoAttribution(ReadModel):
    display_name: str | None
    uri: str | None


class CoverPhoto(ReadModel):
    """A ready-to-load Google cover photo. `url` is good for an <img src> but is
    ephemeral (embeds the key, redirects to a short-lived CDN URL) — never store
    it. `attributions` must be shown with the image. `is_stale` flags a cache
    past the 30-day cap that should be refreshed."""

    url: str
    attributions: list[PhotoAttribution]
    is_stale: bool


class PlaceRead(ReadModel):
    id: uuid.UUID
    name: str | None
    category: str | None
    notes: str | None
    photo_url: str | None
    address: str | None
    latitude: float | None
    longitude: float | None
    timezone: str | None
    area_id: uuid.UUID | None
    area: AreaRead | None
    google_place_id: str | None
    # The raw google_data blob is intentionally not exposed; cover_photo is the
    # supported way to get a venue image (see services.google_places).
    cover_photo: CoverPhoto | None
    google_fetched_at: datetime | None
    created_at: datetime
    updated_at: datetime


class PlacesRefreshRequest(BaseModel):
    """The ids the itinerary flagged as stale (its `stale_place_ids`)."""

    place_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)
    # Override the staleness threshold for this request: refresh any place whose
    # cache is older than this many days. 0 forces a refresh of every id given;
    # omit to use the server default (google_cache_refresh_days).
    refresh_after_days: int | None = Field(default=None, ge=0)


class PlacesRefreshResponse(BaseModel):
    """Current state of the requested places, keyed by id, after a best-effort
    refresh — the client patches its cards (and their cover photos) by id."""

    places: dict[uuid.UUID, PlaceRead]


# --- trips ---


class TripCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    start_date: date
    end_date: date
    cover_image_url: Url | None = None
    # Seeds every day of the trip with this zone; each day can be edited after.
    timezone: TimezoneName | None = None
    # Areas (countries/cities) this trip covers. Deduped; unknown ids 404.
    area_ids: list[uuid.UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def _dates_ordered(self) -> Self:
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self


class TripUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    cover_image_url: Url | None = None
    # Omit to leave areas untouched; a list (even empty) replaces the whole set.
    area_ids: list[uuid.UUID] | None = None


class TripRead(ReadModel):
    id: uuid.UUID
    owner_id: uuid.UUID
    title: str
    description: str | None
    start_date: date
    end_date: date
    cover_image_url: str | None
    # Resolved from the stored area_ids, in order; ids that no longer match an
    # area are dropped (and pruned from storage) so none surface as blanks.
    areas: list[AreaRead] = Field(default_factory=list)
    # The subset of `areas` that have no photo yet. Render the trip now, then —
    # if this is non-empty — call POST /areas/refresh-photos with these ids to
    # fetch covers and patch them in. (Two-step, like places' stale_place_ids.)
    areas_missing_photos: list[uuid.UUID] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class TripDayUpdate(BaseModel):
    """Days are made and removed with the trip's dates; this edits one."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    # First entry is the default zone for items added to the day.
    timezones: list[TimezoneName] | None = Field(default=None, max_length=8)
    place_id: uuid.UUID | None = None

    @field_validator("timezones")
    @classmethod
    def _dedupe(cls, zones: list[str] | None) -> list[str] | None:
        return list(dict.fromkeys(zones)) if zones is not None else None


class TripDayRead(ReadModel):
    date: date
    day_number: int
    name: str | None
    timezones: list[str]
    place: PlaceRead | None
    created_at: datetime
    updated_at: datetime


class TripMemberCreate(BaseModel):
    user_id: uuid.UUID
    role: TripRole = TripRole.MEMBER


class TripMemberRead(ReadModel):
    user: UserRead
    role: TripRole
    created_at: datetime


# --- trip items ---


def resolve_scope_fields(
    scope: TripItemScope,
    requirement: TripItemRequirement | None,
    visibility: TripItemVisibility | None,
) -> tuple[TripItemRequirement | None, TripItemVisibility | None]:
    """Group items carry a requirement, personal items carry a visibility, never
    both. Fills in the default for the missing one and rejects the wrong one."""
    if scope is TripItemScope.GROUP:
        if visibility is not None:
            raise ValueError("visibility only applies to personal items")
        return requirement or TripItemRequirement.OPTIONAL, None
    if requirement is not None:
        raise ValueError("requirement only applies to group items")
    return None, visibility or TripItemVisibility.SHARED


class TransitSegmentCreate(BaseModel):
    """One leg of a transit booking. Times follow the same rules as an item's
    ends (see TripItemCreate); the server settles zones per leg."""

    starts_at: datetime
    start_timezone: TimezoneName | None = None
    ends_at: datetime | None = None
    end_timezone: TimezoneName | None = None
    place_id: uuid.UUID | None = None
    end_place_id: uuid.UUID | None = None
    carrier: str | None = Field(default=None, max_length=120)
    flight_number: str | None = Field(default=None, max_length=40)
    transit_mode: TransitMode | None = None
    confirmation_number: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.ends_at is None and self.end_timezone is not None:
            raise ValueError("end_timezone needs an ends_at")
        if self.transit_mode is TransitMode.MIXED:
            raise ValueError("'mixed' is an item-level summary, not a leg mode")
        return self


class TripItemCreate(BaseModel):
    kind: TripItemKind
    scope: TripItemScope
    # Defaults to optional for group items; must be absent for personal ones.
    requirement: TripItemRequirement | None = None
    # Defaults to shared for personal items; must be absent for group ones.
    visibility: TripItemVisibility | None = None
    title: str = Field(min_length=1, max_length=200)
    notes: str | None = None
    confirmation_number: str | None = Field(default=None, max_length=120)
    # Transit only, and only for a plain hop — a segmented transit's mode is
    # derived from its legs (see the validator). `mixed` is derived, never sent.
    transit_mode: TransitMode | None = None
    tags: list[Tag] = Field(default_factory=list)
    # Local wall time at each end, with the zone it's read in when known: from
    # the request, else the place at that end (the arrival falls back to the
    # departure's zone). With neither the time floats: kept as written, no
    # instant. A naive value is taken as is; an aware one is converted into the
    # zone, so it needs one. Ordering is checked in the route once zones are
    # settled. A multi-leg transit sets these on its `segments` instead, and the
    # item's own span is taken from the first and last leg.
    starts_at: datetime | None = None
    start_timezone: TimezoneName | None = None
    ends_at: datetime | None = None
    end_timezone: TimezoneName | None = None
    image_url: Url | None = None
    place_id: uuid.UUID | None = None
    end_place_id: uuid.UUID | None = None
    # Transit only. Omit for a plain hop (the item's own ends are the one leg);
    # supply two or more to spell out layovers.
    segments: list[TransitSegmentCreate] | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        self.requirement, self.visibility = resolve_scope_fields(
            self.scope, self.requirement, self.visibility
        )
        if self.transit_mode is TransitMode.MIXED:
            raise ValueError("'mixed' is derived from the legs, not set directly")
        if self.transit_mode is not None and self.kind is not TripItemKind.TRANSIT:
            raise ValueError("only transit items have a transit_mode")
        if self.segments is not None:
            if self.kind is not TripItemKind.TRANSIT:
                raise ValueError("only transit items have segments")
            if not self.segments:
                raise ValueError("a transit item needs at least one segment")
            if any(
                value is not None
                for value in (
                    self.starts_at,
                    self.ends_at,
                    self.start_timezone,
                    self.end_timezone,
                    self.place_id,
                    self.end_place_id,
                    self.transit_mode,
                )
            ):
                raise ValueError(
                    "set a segmented transit's times, places, and mode on its segments, not the item"
                )
        else:
            if self.starts_at is None:
                raise ValueError("starts_at is required")
            if self.ends_at is None and self.end_timezone is not None:
                raise ValueError("end_timezone needs an ends_at")
        return self


class TripItemUpdate(BaseModel):
    """Every field from TripItemCreate, all optional. Unknown keys are a 422
    rather than a silent no-op."""

    model_config = ConfigDict(extra="forbid")

    kind: TripItemKind | None = None
    # Changing scope re-homes the item, so the route requires the caller to be
    # allowed to modify both the old and the new form.
    scope: TripItemScope | None = None
    requirement: TripItemRequirement | None = None
    visibility: TripItemVisibility | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    notes: str | None = None
    confirmation_number: str | None = Field(default=None, max_length=120)
    transit_mode: TransitMode | None = None
    tags: list[Tag] | None = None
    starts_at: datetime | None = None
    start_timezone: TimezoneName | None = None
    ends_at: datetime | None = None
    end_timezone: TimezoneName | None = None
    image_url: Url | None = None
    place_id: uuid.UUID | None = None
    end_place_id: uuid.UUID | None = None
    # Replaces the whole leg list when present; only valid on a transit item.
    segments: list[TransitSegmentCreate] | None = None


class ParticipantRead(ReadModel):
    user: UserRead
    status: RsvpStatus
    updated_at: datetime


class RsvpUpdate(BaseModel):
    status: RsvpStatus


class TimeSpanRead(ReadModel):
    """The read side of a models.LocalTimeSpan: wall time at each end (no offset
    in the JSON) with the zone it's read in when known — no zone means a floating
    time, shown as written — plus the same moments as instants for the zoned
    ends, null for floating ones."""

    starts_at: datetime
    start_timezone: str | None
    ends_at: datetime | None
    end_timezone: str | None
    starts_at_utc: datetime | None
    ends_at_utc: datetime | None

    @field_serializer("starts_at_utc", "ends_at_utc")
    def _in_utc(self, value: datetime | None) -> datetime | None:
        # The driver hands back instants in the session's zone; always say Z.
        return value.astimezone(UTC) if value is not None else None


class TransitSegmentRead(TimeSpanRead):
    id: uuid.UUID
    position: int
    place: PlaceRead | None
    end_place: PlaceRead | None
    carrier: str | None
    flight_number: str | None
    transit_mode: TransitMode | None
    confirmation_number: str | None


class TripItemRead(TimeSpanRead):
    id: uuid.UUID
    trip_id: uuid.UUID
    kind: TripItemKind
    scope: TripItemScope
    requirement: TripItemRequirement | None
    visibility: TripItemVisibility | None
    title: str
    notes: str | None
    confirmation_number: str | None
    transit_mode: TransitMode | None
    tags: list[str]
    image_url: str | None
    place: PlaceRead | None
    end_place: PlaceRead | None
    # A transit booking's legs in order (the item's own ends are the first leg's
    # departure and the last leg's arrival); empty for every other kind.
    segments: list[TransitSegmentRead] = Field(
        default_factory=list, validation_alias="transit_segments", serialization_alias="segments"
    )
    created_by: UserRead | None
    # RSVPs on a group item; attendees on a personal one. Callers find their
    # own entry by user id.
    participants: list[ParticipantRead]
    created_at: datetime
    updated_at: datetime


# --- itinerary (the day-by-day view) ---


class LodgingStay(BaseModel):
    """A lodging item as it applies to one day, for the bar at the top of the day."""

    item: TripItemRead
    phase: Literal["check_in", "staying", "check_out"]


class ItineraryEntry(BaseModel):
    """A trip item at one spot in a day's timeline. An item with distinct ends is
    split into one entry per end: `phase` says which this stands for —
    `departure`/`arrival` for transit, `check_in`/`check_out` for lodging — and
    the client leans on that end's time and place. A transit with layovers also
    gets a server-made `layover` entry per stop between its legs, carrying the
    airport it waits at and how long, the same way lodging gets a `check_out`
    card. `phase` is null for everything else."""

    item: TripItemRead
    phase: Literal["departure", "arrival", "check_in", "check_out", "layover"] | None = None
    # Set only on a `layover` entry: where you wait and for how many minutes.
    layover_place: PlaceRead | None = None
    layover_minutes: int | None = None


class ItineraryDay(BaseModel):
    date: date
    day_number: int
    lodging: list[LodgingStay]
    items: list[ItineraryEntry]


class Itinerary(BaseModel):
    trip: TripRead
    days: list[ItineraryDay]
    # Deduped ids of places in this trip whose cached Google data is missing or
    # due a refresh. The client renders the itinerary immediately, then (only if
    # this is non-empty) calls POST /places/refresh with these ids to fill in
    # fresh data and cover photos. Empty when everything is current.
    stale_place_ids: list[uuid.UUID]


# --- mail processing (received email -> trip items) ---


class ReceivedMailRead(ReadModel):
    """A stored received-email receipt, as the retrieve stage returns it. `raw`
    holds the full Resend body for one fetched by id; it's null for ones only
    seen via the list."""

    id: uuid.UUID
    resend_email_id: str
    status: EmailReceiptStatus
    from_address: str | None
    subject: str | None
    message_id: str | None
    received_at: datetime | None
    created_at: datetime
    raw: dict[str, Any] | None = None
