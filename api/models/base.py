from datetime import datetime, timezone

from sqlalchemy import DateTime, TypeDecorator
from sqlalchemy.orm import DeclarativeBase


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UtcDateTime(TypeDecorator):
    """A datetime column that is always timezone-aware UTC on the way out.

    SQLite has no timestamp type — it stores a string and hands back a NAIVE
    datetime regardless of what went in. That matters more than it looks: the
    entitlement check is `pass_expires_at > now(timezone.utc)`, and comparing a
    naive datetime to an aware one raises TypeError rather than returning a wrong
    answer. Every paid request would fail, and only for users who own a pass.

    Binding a naive datetime raises instead of guessing its zone, so "everything
    is UTC" is enforced at the boundary rather than assumed.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                f"naive datetime {value!r} — use shared timestamps in UTC "
                "(api.models.base.utcnow)"
            )
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    """Declarative base.

    All timestamps are timezone-aware UTC, in and out, on every backend. Keeping
    that consistent in the application layer is also what makes a later Postgres
    migration uneventful.
    """

    type_annotation_map = {datetime: UtcDateTime}
