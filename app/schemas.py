import uuid
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    computed_field,
    field_serializer,
    field_validator,
    model_validator,
)

from app.models import (
    CreditEntryKind,
    EmailReceiptStatus,
    LodgingType,
    NotificationCategory,
    NotificationChannel,
    PushPlatform,
    RsvpStatus,
    TransitMode,
    TripItemKind,
    TripItemRequirement,
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


class AdminUserPage(BaseModel):
    """A page of users for the admin dashboard, newest first, plus the total
    across all pages for the pager."""

    items: list[UserRead]
    total: int
    limit: int
    offset: int


class AdminTripRead(ReadModel):
    """A trip row for the admin management dashboard: the trip, its owner's
    email, and how many people are on it."""

    id: uuid.UUID
    title: str
    owner_email: str
    start_date: date
    end_date: date
    member_count: int
    created_at: datetime


class AdminTripPage(BaseModel):
    """A page of trips for the admin dashboard, newest first, plus the total
    across all pages for the pager."""

    items: list[AdminTripRead]
    total: int
    limit: int
    offset: int


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


class TripMemberCreate(BaseModel):
    """A person on the trip, by email. They needn't have an account: the row is
    linked to one as soon as it exists."""

    email: Email
    role: TripRole = TripRole.MEMBER


def _unique_emails(members: list[TripMemberCreate] | None) -> list[TripMemberCreate] | None:
    if members is not None and len({m.email for m in members}) != len(members):
        raise ValueError("each email may appear only once")
    return members


TripMembersIn = Annotated[list[TripMemberCreate], AfterValidator(_unique_emails)]


class TripMemberRead(ReadModel):
    id: uuid.UUID
    email: str
    # Null while no account has this email.
    user: UserRead | None
    role: TripRole
    created_at: datetime


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
    # People to add besides the creator, who is always on as an organizer.
    members: TripMembersIn = Field(default_factory=list)
    # Make this trip an extension of one the caller is on: a full trip of
    # its own (own roster, days, items, bill) that joins the parent's group,
    # or starts one. Its dates are free. 404 if the caller isn't on the parent.
    extends_trip_id: uuid.UUID | None = None
    # The charge POST /trips/quote showed the user. If the server would now
    # charge something else, the trip isn't created (409) so nobody pays a
    # price they didn't see.
    expected_price_cents: int | None = Field(default=None, ge=0)

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
    # Omit to leave the roster untouched; a list replaces it, matched by email:
    # listed people keep their row (role updated), new ones are added, and
    # anyone missing is removed with their personal items and RSVPs. The owner
    # is always kept, as an organizer, whether listed or not.
    members: TripMembersIn | None = None
    # Tell the trip's other members what changed: in their inbox, and as a
    # push if they allow it. Nothing is sent if the edit changes nothing.
    notify_members: bool = False
    # The charge POST /trips/{id}/quote showed the user; 409 if it differs now.
    expected_price_cents: int | None = Field(default=None, ge=0)


class TripRead(ReadModel):
    id: uuid.UUID
    owner_id: uuid.UUID
    title: str
    description: str | None
    start_date: date
    end_date: date
    cover_image_url: str | None
    # Set when this trip is linked to others. `group_id` is the cluster it
    # belongs to; `extends_trip_id` is the trip it was created by extending
    # (null if that trip has since been deleted). Both null for a standalone
    # trip. Read-only here: links are made by extending a trip.
    group_id: uuid.UUID | None = None
    extends_trip_id: uuid.UUID | None = None
    # What the trip costs today, for its length and roster under the rate
    # table it was created with. Not what has been paid for it: see /credits.
    price_cents: int
    # That rate table's key: GET /pricing/schedule?key=... gives its brackets,
    # for pricing an edit locally. New trips get the current one.
    price_schedule: str
    # Resolved from the stored area_ids, in order; ids that no longer match an
    # area are dropped (and pruned from storage) so none surface as blanks.
    areas: list[AreaRead] = Field(default_factory=list)
    # The subset of `areas` that have no photo yet. Render the trip now, then —
    # if this is non-empty — call POST /areas/refresh-photos with these ids to
    # fetch covers and patch them in. (Two-step, like places' stale_place_ids.)
    areas_missing_photos: list[uuid.UUID] = Field(default_factory=list)
    # Everyone on the trip, owner included, in the order they were added.
    members: list[TripMemberRead]
    created_at: datetime
    updated_at: datetime


class TripListEntry(TripRead):
    """One card in the caller's trip list: a trip, plus the caller's other
    trips in its group nested under it. The lead trip is the one none of the
    others were extended from (the earliest, if links were broken).

    Here `start_date`/`end_date` span the caller's whole journey, this trip
    and its extensions together, not the lead trip's own dates; those come
    from GET /trips/{id}. Each extension keeps its own.

    `price_cents` is the lead trip's own price; extensions carry theirs.
    """

    # The caller's other trips in this trip's group (its extensions, and
    # trips extended off those), by start date. Empty for a standalone trip.
    extensions: list[TripRead] = Field(default_factory=list)


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


# --- trip items ---


class ParticipantSet(BaseModel):
    """One person an item is for. `status` is an optional RSVP-style answer;
    leave it out to keep whatever they had (none, for someone new)."""

    member_id: uuid.UUID
    status: RsvpStatus | None = None


def _unique_members(participants: list[ParticipantSet] | None) -> list[ParticipantSet] | None:
    if participants is not None and len({p.member_id for p in participants}) != len(
        participants
    ):
        raise ValueError("each member may appear only once")
    return participants


ParticipantsIn = Annotated[list[ParticipantSet], AfterValidator(_unique_members)]


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


class LodgingDetails(BaseModel):
    """A stay's details beyond its check-in/check-out (the item's times),
    property (the item's place), people (its participants) and rooms (its
    optional `rooms`). Lodging items only; every field optional. On a PATCH the
    object replaces the stored details whole, and null clears them."""

    model_config = ConfigDict(extra="forbid")

    property_type: LodgingType | None = None
    phone: str | None = Field(default=None, min_length=1, max_length=40)
    # Wall time at the property (the stay's check-in zone). An aware value is
    # converted into that zone, so it needs the stay to have one.
    free_cancellation_until: datetime | None = None


def _unique_occupants(occupants: list[uuid.UUID]) -> list[uuid.UUID]:
    if len(set(occupants)) != len(occupants):
        raise ValueError("each member may appear only once")
    return occupants


class LodgingRoomSet(BaseModel):
    """One room of a stay, as sent. Send `id` to keep an existing room (its
    assignments are rewritten from `occupants` either way); leave it out for a
    new one. Every field is optional."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID | None = None
    label: str | None = Field(default=None, min_length=1, max_length=60)
    room_number: str | None = Field(default=None, min_length=1, max_length=40)
    room_type: str | None = Field(default=None, min_length=1, max_length=200)
    guests: int | None = Field(default=None, ge=1, le=999)
    confirmation_number: str | None = Field(default=None, min_length=1, max_length=120)
    # Trip-member ids of who's in it. Anyone listed who isn't a participant
    # yet becomes one. A member can be in only one room of a stay.
    occupants: Annotated[list[uuid.UUID], AfterValidator(_unique_occupants)] = Field(
        default_factory=list
    )


def _rooms_consistent(rooms: list[LodgingRoomSet] | None) -> list[LodgingRoomSet] | None:
    if rooms is None:
        return rooms
    ids = [room.id for room in rooms if room.id is not None]
    if len(set(ids)) != len(ids):
        raise ValueError("each room may appear only once")
    members = [member for room in rooms for member in room.occupants]
    if len(set(members)) != len(members):
        raise ValueError("a member can be in only one room")
    return rooms


RoomsIn = Annotated[list[LodgingRoomSet], Field(max_length=50), AfterValidator(_rooms_consistent)]


class TripItemCreate(BaseModel):
    kind: TripItemKind
    # How much the participants are expected to be there; a label only.
    requirement: TripItemRequirement | None = None
    # What people who aren't participants see. Participants always see it all.
    visibility: TripItemVisibility = TripItemVisibility.OPEN
    # Needed unless the item has a place (for a segmented transit, its first
    # leg's), whose name then stands in for it.
    title: str | None = Field(default=None, min_length=1, max_length=200)
    notes: str | None = None
    confirmation_number: str | None = Field(default=None, max_length=120)
    # The agent's/booking site's reference when booked through one; shared by
    # every item that booking covers.
    booking_number: str | None = Field(default=None, max_length=120)
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
    # Lodging only: the stay's room, guests, cancellation deadline, etc.
    lodging: LodgingDetails | None = None
    # Lodging only, optional: the rooms one by one and who's in each.
    rooms: RoomsIn | None = None
    # Who the item is for, by trip member. Nobody is implied: an item for the
    # whole trip lists every member. Omit to make it just for the creator.
    participants: ParticipantsIn | None = None
    # Tell the trip's other members about it, as far as they can see the item
    # (see app.services.item_notifications): in their inbox, and as a push if
    # they allow it.
    notify_members: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.transit_mode is TransitMode.MIXED:
            raise ValueError("'mixed' is derived from the legs, not set directly")
        if self.transit_mode is not None and self.kind is not TripItemKind.TRANSIT:
            raise ValueError("only transit items have a transit_mode")
        if self.lodging is not None and self.kind is not TripItemKind.LODGING:
            raise ValueError("only lodging items have lodging details")
        if self.rooms and self.kind is not TripItemKind.LODGING:
            raise ValueError("only lodging items have rooms")
        if self.rooms and any(room.id is not None for room in self.rooms):
            raise ValueError("a new item's rooms are new: leave out their ids")
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
    requirement: TripItemRequirement | None = None
    visibility: TripItemVisibility | None = None
    # null drops the title, as long as the item keeps a place.
    title: str | None = Field(default=None, min_length=1, max_length=200)
    notes: str | None = None
    confirmation_number: str | None = Field(default=None, max_length=120)
    booking_number: str | None = Field(default=None, max_length=120)
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
    # Replaces the stay's details when present (null clears them); only valid
    # on a lodging item. Changing the kind away from lodging drops them.
    lodging: LodgingDetails | None = None
    # Replaces the stay's rooms and who's in each when present ([] or null
    # clears them; rooms sent with an `id` are kept and updated). Leave it out
    # and the rooms are untouched. Changing the kind away from lodging drops
    # them.
    rooms: RoomsIn | None = None
    # Replaces who the item is for when present: members left out are dropped,
    # listed ones keep their answer unless a new status is sent.
    participants: ParticipantsIn | None = None
    # Tell the trip's other members what changed, as with TripItemCreate.
    # Nothing is sent if the edit changes nothing.
    notify_members: bool = False


class ParticipantRead(ReadModel):
    member: TripMemberRead
    status: RsvpStatus | None
    # Which of the stay's `rooms` they're in; null when not said (and always
    # for other kinds).
    room_id: uuid.UUID | None = None
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


class LodgingRead(ReadModel):
    property_type: LodgingType | None
    phone: str | None
    free_cancellation_until: datetime | None


class LodgingRoomRead(ReadModel):
    id: uuid.UUID
    position: int
    label: str | None
    room_number: str | None
    room_type: str | None
    guests: int | None
    confirmation_number: str | None
    # Trip-member ids of who's in it (match against `participants[].member.id`).
    occupants: list[uuid.UUID] = Field(validation_alias="occupant_ids")


class TripItemRead(TimeSpanRead):
    id: uuid.UUID
    trip_id: uuid.UUID
    kind: TripItemKind
    visibility: TripItemVisibility
    # True when the caller isn't a participant on a closed item: they get its
    # kind, title and times, and every other detail below is blanked.
    redacted: bool = False
    requirement: TripItemRequirement | None
    # As written; null when the item goes by its place.
    title: str | None
    # What to show as its name: the title, else the place's name, else the kind
    # ("Activity"). On a redacted item, whose place is hidden, it skips the place.
    display_title: str
    notes: str | None
    # The provider's own reference (the hotel's, the airline's PNR).
    confirmation_number: str | None
    # The agent's/booking site's reference, when booked through one.
    booking_number: str | None
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
    # A stay's details; null for every other kind, and for a stay with none.
    lodging: LodgingRead | None = None
    # A stay's rooms in order, when anyone has spelled them out; usually empty.
    rooms: list[LodgingRoomRead] = Field(default_factory=list)
    created_by: UserRead | None
    # Who the item is for. Callers find their own entry by member.user.id.
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
    # The layover's IANA zone — the arriving leg's end zone when it agrees with the
    # departing leg's start zone (you land and take off at the same airport). Comes
    # straight from the segment zones, so it's available even when the layover has
    # no resolved place (the mail/image-import case). Null if the two disagree or
    # either is unset.
    layover_timezone: str | None = None


class ItineraryDay(BaseModel):
    date: date
    # Counted from the first day of the caller's journey: the earliest start
    # among the trips merged into the itinerary.
    day_number: int
    # The trip this date belongs to. Where two of the caller's trips share
    # it, the parent (the one the other was extended from). Null for a date
    # none of them covers, e.g. one widened to show an item outside the trip.
    trip_id: uuid.UUID | None
    # Every one of the caller's trips covering the date; more than one is an
    # overlap. Each item names its own trip by `item.trip_id`.
    trip_ids: list[uuid.UUID]
    # A date between the caller's trips with nothing on it: none of them
    # covers it and it has no items or lodging. Kept as a day so numbering
    # and dates stay continuous; clients show it as a break, not a page.
    # A date outside the trips that does have something on it (say, a
    # flight the day before) is not a gap.
    is_gap: bool
    lodging: list[LodgingStay]
    items: list[ItineraryEntry]


class Itinerary(BaseModel):
    """The caller's merged day-by-day view of a trip and every other trip in
    its group they're on: the same days whichever of those trips is asked for."""

    # The trip asked for.
    trip: TripRead
    # Every trip merged into this view, by start date (just `trip` when it
    # stands alone).
    trips: list[TripRead]
    days: list[ItineraryDay]
    # Deduped ids of places in this trip whose cached Google data is missing or
    # due a refresh. The client renders the itinerary immediately, then (only if
    # this is non-empty) calls POST /places/refresh with these ids to fill in
    # fresh data and cover photos. Empty when everything is current.
    stale_place_ids: list[uuid.UUID]


# --- mail processing (received email -> trip items) ---


class ReceivedMailRead(ReadModel):
    """A stored received-email receipt. `raw` holds the full Resend body once
    fetched by id (null for list-only rows); `user_id`/`extracted`/`extracted_at`
    are filled by the extract stage (null until then)."""

    id: uuid.UUID
    resend_email_id: str
    status: EmailReceiptStatus
    from_address: str | None
    subject: str | None
    message_id: str | None
    received_at: datetime | None
    created_at: datetime
    user_id: uuid.UUID | None = None
    trip_id: uuid.UUID | None = None
    extracted_at: datetime | None = None
    raw: dict[str, Any] | None = None
    extracted: dict[str, Any] | None = None
    failures: list[dict[str, Any]] = Field(default_factory=list)


class ImageReceiptRead(ReadModel):
    """A stored uploaded image's metadata (the bytes themselves aren't returned).
    `extracted`/`extracted_at`/`trip_id` are filled by the later extract/import
    stages (null until then)."""

    id: uuid.UUID
    user_id: uuid.UUID | None = None
    trip_id: uuid.UUID | None = None
    status: EmailReceiptStatus
    content_type: str
    filename: str | None
    byte_size: int
    sha256: str
    created_at: datetime
    extracted_at: datetime | None = None
    extracted: dict[str, Any] | None = None
    failures: list[dict[str, Any]] = Field(default_factory=list)


class ImportResult(ReadModel):
    """The outcome of importing one receipt: `placed` (into `trip_id`,
    `item_count` items created), `no_match`/`ambiguous` (left `extracted`; for
    ambiguous, `candidate_trip_ids` lists the overlapping trips), or `skipped`
    (the receipt wasn't in an importable state)."""

    receipt: ReceivedMailRead
    outcome: Literal["placed", "no_match", "ambiguous", "skipped"]
    trip_id: uuid.UUID | None = None
    item_count: int = 0
    candidate_trip_ids: list[uuid.UUID] = Field(default_factory=list)


class ImageImportResult(ReadModel):
    """The outcome of importing one uploaded image — same shape as `ImportResult`,
    but `receipt` is the image's metadata rather than an email's."""

    receipt: ImageReceiptRead
    outcome: Literal["placed", "no_match", "ambiguous", "skipped"]
    trip_id: uuid.UUID | None = None
    item_count: int = 0
    candidate_trip_ids: list[uuid.UUID] = Field(default_factory=list)


# --- pricing & credits ---


class BracketRead(BaseModel):
    # Trip days in `trip_brackets`, extra member-days in `member_brackets`.
    first_day: int
    # Null: open-ended ("day 31 and beyond").
    last_day: int | None
    cents_per_day: int


class PricingScheduleRead(BaseModel):
    """The rate tables, for showing how pricing works. A trip day is charged
    at the rate of the bracket it falls in; that covers up to
    `included_members` people. People beyond are charged by the member-day
    (one extra person for one day): all the trip's extra member-days
    together, each at the rate of the `member_brackets` bracket it falls in."""

    key: str
    included_members: int
    trip_brackets: list[BracketRead]
    member_brackets: list[BracketRead]


class BracketLineRead(BaseModel):
    """One row of a price breakdown: the days that fell in one bracket. In
    `member_lines` the days are extra member-days, and `cents` is already
    the total for everyone (not per person)."""

    first_day: int
    last_day: int
    days: int
    cents_per_day: int
    cents: int


class TripQuote(BaseModel):
    """What a create or edit would cost. `price_cents` is what the caller
    would be charged (0 if nothing is due); send it back as
    `expected_price_cents` with the write. `refund_cents` is what would go
    back to the trip's owner instead, if the edit lowers the price inside
    the refund window."""

    schedule: str
    days: int
    members: int
    extra_members: int
    owed_cents: int
    paid_cents: int
    price_cents: int
    refund_cents: int
    refund_window_open: bool
    trip_lines: list[BracketLineRead]
    member_lines: list[BracketLineRead]
    balance_cents: int
    shortfall_cents: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def member_days(self) -> int:
        """extra_members × days: what `member_lines` breaks down."""
        return self.extra_members * self.days


class CreditTripRef(ReadModel):
    """The trip a ledger row is about, as it is now."""

    id: uuid.UUID
    title: str
    start_date: date
    end_date: date


class CreditPartyRead(ReadModel):
    """Someone else's side of a ledger row (or the reader's own: compare ids)."""

    id: uuid.UUID
    display_name: str


class CreditEntryRead(ReadModel):
    id: uuid.UUID
    kind: CreditEntryKind
    # Positive when credits came in, negative when spent.
    amount_cents: int
    # The balance right after this row.
    balance_after_cents: int
    # Null for grants and purchases. Kept after the trip is deleted.
    trip_id: uuid.UUID | None
    # The trip as it is now, for linking to it: null once it's deleted or
    # the reader is no longer on it (`title` still says what it was).
    trip: CreditTripRef | None
    # On a refund: the charge it gives back, and who paid that charge. Refunds
    # all go to the trip's owner, so it may have been someone else's payment.
    refund_of_id: uuid.UUID | None
    refund_of_payer: CreditPartyRead | None
    # On a charge: how much of it has been refunded so far, and to whom
    # (the trip's owner, who isn't necessarily the payer).
    refunded_cents: int
    refunded_to: list[CreditPartyRead]
    description: str | None
    created_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def title(self) -> str:
        """A line for the ledger: "Unlocked Lisbon", "Refund: Lisbon"."""
        match self.kind:
            case CreditEntryKind.GRANT:
                return self.description or "Credits added"
            case CreditEntryKind.PURCHASE:
                return "Credits purchased"
            case CreditEntryKind.TRIP_CHARGE:
                return f"Unlocked {self.description}" if self.description else "Unlocked a trip"
            case CreditEntryKind.TRIP_REFUND:
                return f"Refund: {self.description}" if self.description else "Refund"


class CreditsRead(BaseModel):
    """The balance, and a page of the ledger, newest first. Pass
    `next_cursor` as `before` for the next page; null when there's no more."""

    balance_cents: int
    entries: list[CreditEntryRead]
    next_cursor: uuid.UUID | None


class CreditPurchaseCreate(BaseModel):
    """A completed App Store purchase of credits, as StoreKit signed it
    (`VerificationResult.jwsRepresentation`)."""

    signed_transaction: str = Field(min_length=1, max_length=16_384)


class CreditGrant(BaseModel):
    """Credits for one person, by id or email (exactly one)."""

    user_id: uuid.UUID | None = None
    email: Email | None = None
    amount_cents: int = Field(gt=0, le=10_000_000)
    # Shown to them in their ledger.
    description: str | None = Field(default=None, max_length=200)
    # A receipt or reference, if any; the same one can't be granted twice.
    external_id: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def _one_recipient(self) -> Self:
        if (self.user_id is None) == (self.email is None):
            raise ValueError("give exactly one of user_id or email")
        return self


class CreditGrantResult(BaseModel):
    entry: CreditEntryRead
    balance_cents: int


# --- notifications ---


class NotificationRead(ReadModel):
    """One inbox entry. `type` says what happened (e.g. "trip_item.updated") and
    `data` carries what the client needs to render and deep-link it; the trip
    and item ids go null if those are deleted, leaving the text as written.
    `announcement_id` is set on broadcasts. Ordered by `visible_at`, which is
    when it arrived in the inbox."""

    id: uuid.UUID
    category: NotificationCategory
    type: str
    title: str
    body: str | None
    data: dict[str, Any]
    actor: UserRead | None
    trip_id: uuid.UUID | None
    trip_item_id: uuid.UUID | None
    announcement_id: uuid.UUID | None
    visible_at: datetime
    expires_at: datetime | None
    read_at: datetime | None
    created_at: datetime

    @computed_field
    @property
    def read(self) -> bool:
        return self.read_at is not None


class NotificationPage(BaseModel):
    """A page of the inbox, newest first. Pass `next_cursor` as `before` for
    the next one; null when there's no more."""

    items: list[NotificationRead]
    next_cursor: uuid.UUID | None


class UnreadCount(BaseModel):
    count: int


class NotificationSend(BaseModel):
    """A notification sent by hand to particular accounts, by id or email (or
    both). It isn't from anyone: no actor, so sending one to yourself works.
    `visible_at` schedules it; `dedupe_key` rewrites an earlier send under the
    same key instead of adding another."""

    user_ids: list[uuid.UUID] = Field(default_factory=list)
    emails: list[Email] = Field(default_factory=list)
    category: NotificationCategory
    type: str = Field(default="message", min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    body: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    trip_id: uuid.UUID | None = None
    trip_item_id: uuid.UUID | None = None
    visible_at: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None
    dedupe_key: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def _has_recipients(self) -> Self:
        if not self.user_ids and not self.emails:
            raise ValueError("name at least one recipient in user_ids or emails")
        return self


class NotificationSendResult(BaseModel):
    """`recipients` accounts were named; `sent` of them got it (the rest have
    the category switched off)."""

    recipients: int
    sent: int


class NotificationPreferenceSet(BaseModel):
    category: NotificationCategory
    channel: NotificationChannel
    enabled: bool


class NotificationPreferenceRead(NotificationPreferenceSet):
    # Fixed on: sending it disabled is refused.
    locked: bool


class PushDeviceCreate(BaseModel):
    platform: PushPlatform
    token: str = Field(min_length=1, max_length=4096)
    app_version: str | None = Field(default=None, max_length=40)
    locale: str | None = Field(default=None, max_length=35)
    timezone: TimezoneName | None = None


class PushDeviceRead(ReadModel):
    id: uuid.UUID
    platform: PushPlatform
    app_version: str | None
    locale: str | None
    timezone: str | None
    last_seen_at: datetime
    created_at: datetime


BroadcastCategory = Literal[NotificationCategory.ANNOUNCEMENT, NotificationCategory.PROMOTION]


class AnnouncementCreate(BaseModel):
    """A broadcast to every user. It shows from `starts_at` (now when omitted)
    until `ends_at` (open-ended when omitted), including for people who sign
    up meanwhile. `type` defaults to the category."""

    category: BroadcastCategory
    type: str | None = Field(default=None, min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    body: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _window(self) -> Self:
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        return self


class AnnouncementUpdate(BaseModel):
    """Edits reach copies already in inboxes. Set `ends_at` to now to take one
    down while keeping it on record."""

    category: BroadcastCategory | None = None
    type: str | None = Field(default=None, min_length=1, max_length=64)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = None
    data: dict[str, Any] | None = None
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None


class AnnouncementRead(ReadModel):
    id: uuid.UUID
    category: NotificationCategory
    type: str
    title: str
    body: str | None
    data: dict[str, Any]
    starts_at: datetime
    ends_at: datetime | None
    created_by_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
