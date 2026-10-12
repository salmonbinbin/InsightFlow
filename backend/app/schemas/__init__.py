from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


UTCDateTime = Annotated[datetime, AfterValidator(as_utc)]

__all__ = ["UTCDateTime"]
