import base64
from binascii import Error as Base64Error
from datetime import UTC, datetime
from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, field_validator


class JobHistoryCursor(BaseModel):
    """A position in the history order; ownership is checked on every query."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    created_at: AwareDatetime
    id: UUID

    @field_validator("created_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        try:
            return value.astimezone(UTC)
        except (OverflowError, ValueError) as error:
            raise ValueError("Cursor timestamp is outside the UTC range") from error

    def encode(self) -> str:
        payload = self.model_dump_json().encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, value: str) -> Self:
        try:
            payload = base64.b64decode(
                value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
            )
            return cls.model_validate_json(payload)
        except (Base64Error, ValueError) as error:
            raise ValueError("Invalid job history cursor") from error
