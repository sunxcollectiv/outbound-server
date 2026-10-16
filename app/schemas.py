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
    RsvpStatus,
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
    photo_url: Url | None = None


class AreaCreate(AreaBase):
    name: str = Field(min_length=1, max_length=200)


class AreaUpdate(AreaBase):
    name: str | None = Field(default=None, min_length=1, max_length=200)


class AreaRead(ReadModel):
    id: uuid.UUID
    name: str
    photo_url: str | None
    created_at: datetime
    updated_at: datetime


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
    google_data: dict[str, Any] | None
    google_fetched_at: datetime | None
    created_at: datetime
    updated_at: datetime


# --- trips ---


class TripCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    start_date: date
    end_date: date
    cover_image_url: Url | None = None
    # Seeds every day of the trip with this zone; each day can be edited after.
    timezone: TimezoneName | None = None

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


class TripRead(ReadModel):
    id: uuid.UUID
    owner_id: uuid.UUID
    title: str
    description: str | None
    start_date: date
    end_date: date
    cover_image_url: str | None
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


class TripItemCreate(BaseModel):
    kind: TripItemKind
    scope: TripItemScope
    # Defaults to optional for group items; must be absent for personal ones.
    requirement: TripItemRequirement | None = None
    # Defaults to shared for personal items; must be absent for group ones.
    visibility: TripItemVisibility | None = None
    title: str = Field(min_length=1, max_length=200)
    notes: str | None = None
    tags: list[Tag] = Field(default_factory=list)
    # Local wall time at each end, with the zone it's read in when known: from
    # the request, else the place at that end (the arrival falls back to the
    # departure's zone). With neither the time floats: kept as written, no
    # instant. A naive value is taken as is; an aware one is converted into the
    # zone, so it needs one. Ordering is checked in the route once zones are
    # settled.
    starts_at: datetime
    start_timezone: TimezoneName | None = None
    ends_at: datetime | None = None
    end_timezone: TimezoneName | None = None
    image_url: Url | None = None
    place_id: uuid.UUID | None = None
    end_place_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        self.requirement, self.visibility = resolve_scope_fields(
            self.scope, self.requirement, self.visibility
        )
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
    tags: list[Tag] | None = None
    starts_at: datetime | None = None
    start_timezone: TimezoneName | None = None
    ends_at: datetime | None = None
    end_timezone: TimezoneName | None = None
    image_url: Url | None = None
    place_id: uuid.UUID | None = None
    end_place_id: uuid.UUID | None = None


class ParticipantRead(ReadModel):
    user: UserRead
    status: RsvpStatus
    updated_at: datetime


class RsvpUpdate(BaseModel):
    status: RsvpStatus


class TripItemRead(ReadModel):
    id: uuid.UUID
    trip_id: uuid.UUID
    kind: TripItemKind
    scope: TripItemScope
    requirement: TripItemRequirement | None
    visibility: TripItemVisibility | None
    title: str
    notes: str | None
    tags: list[str]
    # Wall time at each end (no offset in the JSON) and, when known, the zone
    # it's read in. No zone means a floating time: show it as written and pick
    # a zone (the trip day's, say) only when an instant is needed.
    starts_at: datetime
    start_timezone: str | None
    ends_at: datetime | None
    end_timezone: str | None
    # The same moments as instants for the zoned ends; null for floating ones.
    starts_at_utc: datetime | None
    ends_at_utc: datetime | None
    image_url: str | None
    place: PlaceRead | None
    end_place: PlaceRead | None
    created_by: UserRead | None
    # RSVPs on a group item; attendees on a personal one. Callers find their
    # own entry by user id.
    participants: list[ParticipantRead]
    created_at: datetime
    updated_at: datetime

    @field_serializer("starts_at_utc", "ends_at_utc")
    def _in_utc(self, value: datetime | None) -> datetime | None:
        # The driver hands back instants in the session's zone; always say Z.
        return value.astimezone(UTC) if value is not None else None


# --- itinerary (the day-by-day view) ---


class LodgingStay(BaseModel):
    """A lodging item as it applies to one day, for the bar at the top of the day."""

    item: TripItemRead
    phase: Literal["check_in", "staying", "check_out"]


class ItineraryEntry(BaseModel):
    """A trip item at one spot in a day's timeline. A transit item is split into
    two entries, one per end: `transit_phase` says which this stands for and the
    client leans on that end's time and place. It's null for everything else."""

    item: TripItemRead
    transit_phase: Literal["departure", "arrival"] | None = None


class ItineraryDay(BaseModel):
    date: date
    day_number: int
    lodging: list[LodgingStay]
    items: list[ItineraryEntry]


class Itinerary(BaseModel):
    trip: TripRead
    days: list[ItineraryDay]
