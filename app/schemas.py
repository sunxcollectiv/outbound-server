import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal, Self
from zoneinfo import ZoneInfo

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
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


def _valid_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except Exception as exc:  # ZoneInfo raises several unrelated types for bad keys
        raise ValueError(f"unknown IANA timezone: {value!r}") from exc
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


# --- locations ---


class LocationBase(BaseModel):
    category: str | None = Field(default=None, max_length=60)
    notes: str | None = None
    photo_url: Url | None = None
    address: str | None = Field(default=None, max_length=500)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    timezone: TimezoneName | None = None


class LocationCreate(LocationBase):
    name: str = Field(min_length=1, max_length=200)
    google_place_id: str | None = Field(default=None, min_length=1, max_length=255)


class LocationUpdate(LocationBase):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    google_place_id: str | None = Field(default=None, min_length=1, max_length=255)


class LocationFromGoogle(BaseModel):
    google_place_id: str = Field(min_length=1, max_length=255)


class LocationRead(ReadModel):
    id: uuid.UUID
    name: str
    category: str | None
    notes: str | None
    photo_url: str | None
    address: str | None
    latitude: float | None
    longitude: float | None
    timezone: str | None
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


class TripMemberCreate(BaseModel):
    user_id: uuid.UUID
    role: TripRole = TripRole.MEMBER


class TripMemberRead(ReadModel):
    user: UserRead
    role: TripRole
    created_at: datetime


# --- trip items ---


def check_scope_fields(
    scope: TripItemScope,
    requirement: TripItemRequirement | None,
    visibility: TripItemVisibility | None,
) -> None:
    """Group items carry a requirement, personal items carry a visibility, never both."""
    if scope is TripItemScope.GROUP and visibility is not None:
        raise ValueError("visibility only applies to personal items")
    if scope is TripItemScope.PERSONAL and requirement is not None:
        raise ValueError("requirement only applies to group items")


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
    starts_at: AwareDatetime
    ends_at: AwareDatetime | None = None
    # Local zone for day bucketing; falls back to the location's, then UTC.
    timezone: TimezoneName | None = None
    image_url: Url | None = None
    location_id: uuid.UUID | None = None
    end_location_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        check_scope_fields(self.scope, self.requirement, self.visibility)
        if self.scope is TripItemScope.GROUP and self.requirement is None:
            self.requirement = TripItemRequirement.OPTIONAL
        if self.scope is TripItemScope.PERSONAL and self.visibility is None:
            self.visibility = TripItemVisibility.SHARED
        if self.ends_at is not None and self.ends_at < self.starts_at:
            raise ValueError("ends_at must be at or after starts_at")
        return self


class TripItemUpdate(BaseModel):
    """scope is fixed at creation and deliberately absent here; extra="forbid"
    turns an attempt to change it into a 422 instead of a silent no-op."""

    model_config = ConfigDict(extra="forbid")

    kind: TripItemKind | None = None
    requirement: TripItemRequirement | None = None
    visibility: TripItemVisibility | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    notes: str | None = None
    tags: list[Tag] | None = None
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None
    timezone: TimezoneName | None = None
    image_url: Url | None = None
    location_id: uuid.UUID | None = None
    end_location_id: uuid.UUID | None = None


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
    starts_at: datetime
    ends_at: datetime | None
    timezone: str | None
    image_url: str | None
    location: LocationRead | None
    end_location: LocationRead | None
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


class ItineraryDay(BaseModel):
    date: date
    day_number: int
    lodging: list[LodgingStay]
    items: list[TripItemRead]


class Itinerary(BaseModel):
    trip: TripRead
    days: list[ItineraryDay]
