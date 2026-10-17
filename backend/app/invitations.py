"""Issue registration invitations through an owner-only database command."""

import argparse
import json
import secrets
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.repositories import invitations, users

DEFAULT_EXPIRY_DAYS = 7
MAX_EXPIRY_DAYS = 30
_email_adapter = TypeAdapter(EmailStr)


class EmailAlreadyRegisteredError(ValueError):
    """An existing account does not need a registration invitation."""


@dataclass(frozen=True)
class IssuedInvitation:
    id: UUID
    email: str
    code: str = field(repr=False)
    created_at: datetime
    expires_at: datetime


def normalize_invitation_email(email: str) -> str:
    # Match RegisterRequest's EmailStr validation and the endpoint's normalization.
    return str(_email_adapter.validate_python(email)).strip().lower()


def hash_invitation_code(code: str) -> str:
    # Codes contain 32 random bytes, rather than a human-chosen password.
    return sha256(code.encode("utf-8")).hexdigest()


def _validate_expiry_days(days: int) -> None:
    if type(days) is not int or not 1 <= days <= MAX_EXPIRY_DAYS:
        raise ValueError(
            f"expires_in_days must be an integer from 1 to {MAX_EXPIRY_DAYS}"
        )


def issue_invitation(
    session: Session,
    email: str,
    *,
    expires_in_days: int = DEFAULT_EXPIRY_DAYS,
) -> IssuedInvitation:
    """Issue within the caller's transaction; share the code only after commit.

    Each call creates an independent invitation. Reissuing for the same address
    does not revoke earlier codes. This operation neither creates a user nor
    redeems an invitation.
    """
    normalized_email = normalize_invitation_email(email)
    _validate_expiry_days(expires_in_days)
    if users.get_user_by_email(session, normalized_email) is not None:
        raise EmailAlreadyRegisteredError("Email is already registered")
    code = secrets.token_urlsafe(32)
    created_at = datetime.now(UTC)
    expires_at = created_at + timedelta(days=expires_in_days)
    invitation = invitations.create_invitation(
        session,
        email=normalized_email,
        code_hash=hash_invitation_code(code),
        created_at=created_at,
        expires_at=expires_at,
    )
    return IssuedInvitation(
        invitation.id, normalized_email, code, created_at, expires_at
    )


def _email_argument(value: str) -> str:
    try:
        return normalize_invitation_email(value)
    except ValidationError as error:
        raise argparse.ArgumentTypeError("must be a valid email address") from error


def _days_argument(value: str) -> int:
    try:
        days = int(value)
        _validate_expiry_days(days)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            f"must be an integer from 1 to {MAX_EXPIRY_DAYS}"
        ) from error
    return days


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="Issue one email-bound invitation")
    create.add_argument("email", type=_email_argument)
    create.add_argument(
        "--expires-in-days", type=_days_argument, default=DEFAULT_EXPIRY_DAYS
    )
    args = parser.parse_args(argv)

    engine = None
    try:
        # Validate arguments before initializing any configured database path.
        # This command never starts FastAPI or imports the inference packages.
        from app.database import SessionLocal, commit_session, engine

        with SessionLocal() as session:
            issued = issue_invitation(
                session, args.email, expires_in_days=args.expires_in_days
            )
            commit_session(session)
    except EmailAlreadyRegisteredError:
        print("Cannot invite an email that is already registered.", file=sys.stderr)
        return 1
    except (SQLAlchemyError, RuntimeError, OSError):
        # Do not print SQL parameters or an invitation on a failed transaction.
        print(
            "Could not create invitation. Check database settings, permissions and Alembic migrations.",
            file=sys.stderr,
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    print(
        json.dumps(
            {
                "id": str(issued.id),
                "email": issued.email,
                "code": issued.code,
                "created_at": issued.created_at.isoformat(),
                "expires_at": issued.expires_at.isoformat(),
            }
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
