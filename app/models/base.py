import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, TypeDecorator, func
from sqlalchemy.orm import Mapped, mapped_column


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)


class LocalDateTime(TypeDecorator[datetime]):
    """A naive wall-clock time, paired with a timezone column elsewhere on the row.

    psycopg binds an aware value to a `timestamp` column by silently shifting it
    into the session's zone, so this refuses aware values outright: callers
    convert with app.services.timekeeping.to_local first.
    """

    impl = DateTime(timezone=False)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is not None and value.tzinfo is not None:
            raise ValueError("LocalDateTime columns take naive local times; convert first")
        return value


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


def str_enum(enum_cls: type[StrEnum], name: str) -> Enum:
    """VARCHAR + CHECK rather than a native Postgres enum, so adding a value
    is a constraint swap instead of an ALTER TYPE."""
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda cls: [member.value for member in cls],
    )
