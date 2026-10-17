import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import URL, create_engine, inspect, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker

import app.database as database
import app.invitations as invitations
from app.database import enable_sqlite_foreign_keys
from app.db_models import Base, RegistrationInvitation, User
from tests.factories import create_test_user


@pytest.fixture(params=["models", "migration"])
def invitation_sessions(request, tmp_path, monkeypatch):
    url = URL.create("sqlite", database=str(tmp_path / "invitations.db"))
    if request.param == "migration":
        monkeypatch.setenv("ALEMBIC_DATABASE_URL", str(url))
        command.upgrade(Config("alembic.ini"), "head")
    engine = create_engine(url)
    enable_sqlite_foreign_keys(engine)
    if request.param == "models":
        Base.metadata.create_all(engine)
    try:
        yield sessionmaker(engine)
    finally:
        engine.dispose()


def test_issuance_persists_only_a_hash_with_normalized_email_and_utc_expiry(
    invitation_sessions,
):
    with invitation_sessions() as session:
        issued = invitations.issue_invitation(session, " Friend@Example.COM ")
        session.commit()
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", issued.code)
    assert issued.code not in repr(issued)
    with invitation_sessions() as session:
        stored = session.get(RegistrationInvitation, issued.id)
        assert stored.email == issued.email == "friend@example.com"
        assert stored.code_hash == sha256(issued.code.encode()).hexdigest()
        assert stored.code_hash != issued.code
        assert stored.created_at == issued.created_at
        assert stored.expires_at == issued.expires_at
        assert stored.expires_at - stored.created_at == timedelta(days=7)
        assert stored.created_at.tzinfo is UTC and stored.expires_at.tzinfo is UTC
        assert stored.used_at is None
        assert session.scalars(select(User)).all() == []


def test_issuance_does_not_commit_the_callers_transaction(invitation_sessions):
    with invitation_sessions() as session:
        invitations.issue_invitation(session, "friend@example.com")
        session.rollback()
    with invitation_sessions() as session:
        assert session.scalars(select(RegistrationInvitation)).all() == []


def test_reissuing_creates_independent_codes_without_revoking_previous_one(
    invitation_sessions,
):
    with invitation_sessions() as session:
        first = invitations.issue_invitation(session, "friend@example.com")
        second = invitations.issue_invitation(
            session, "friend@example.com", expires_in_days=1
        )
        session.commit()
    assert first.id != second.id and first.code != second.code
    with invitation_sessions() as session:
        rows = session.scalars(select(RegistrationInvitation)).all()
        assert {row.id for row in rows} == {first.id, second.id}
        assert all(row.used_at is None for row in rows)
        assert (
            session.get(RegistrationInvitation, first.id).expires_at == first.expires_at
        )
        assert second.expires_at - second.created_at == timedelta(days=1)


def test_cannot_issue_an_invitation_for_a_registered_email(invitation_sessions):
    with invitation_sessions() as session:
        user = create_test_user(session, email="friend@example.com")
        user_id = user.id
        session.commit()
        with pytest.raises(invitations.EmailAlreadyRegisteredError):
            invitations.issue_invitation(session, "FRIEND@Example.COM")
    with invitation_sessions() as session:
        assert session.scalars(select(RegistrationInvitation)).all() == []
        assert session.scalars(select(User.id)).all() == [user_id]


@pytest.mark.parametrize("email", ["", "not-an-email", "friend@localhost"])
def test_invalid_email_creates_no_invitation(invitation_sessions, email):
    with invitation_sessions() as session:
        with pytest.raises(ValidationError):
            invitations.issue_invitation(session, email)
        assert session.scalars(select(RegistrationInvitation)).all() == []


@pytest.mark.parametrize("days", [0, -1, 31, True, 1.5, "7"])
def test_invalid_lifetime_creates_no_invitation(invitation_sessions, days):
    with invitation_sessions() as session:
        with pytest.raises(ValueError, match="expires_in_days"):
            invitations.issue_invitation(
                session, "friend@example.com", expires_in_days=days
            )
        assert session.scalars(select(RegistrationInvitation)).all() == []


def test_maximum_invitation_lifetime_is_accepted(invitation_sessions):
    with invitation_sessions() as session:
        issued = invitations.issue_invitation(
            session, "friend@example.com", expires_in_days=30
        )
        session.commit()
    assert issued.expires_at - issued.created_at == timedelta(days=30)


def test_database_rejects_duplicate_code_hash_and_preserves_first_invitation(
    invitation_sessions, monkeypatch
):
    monkeypatch.setattr(
        invitations.secrets, "token_urlsafe", lambda _: "synthetic-code"
    )
    with invitation_sessions() as session:
        first = invitations.issue_invitation(session, "first@example.com")
        session.commit()
        with pytest.raises(IntegrityError):
            invitations.issue_invitation(session, "second@example.com")
        session.rollback()
    with invitation_sessions() as session:
        assert session.scalars(select(RegistrationInvitation.id)).all() == [first.id]


@pytest.mark.parametrize(
    "changes",
    [
        {"email": "UPPER@example.com"},
        {"email": " friend@example.com"},
        {"email": ""},
        {"code_hash": "a" * 63},
        {"code_hash": "G" * 64},
        {"code_hash": "A" * 64},
        {"expires_at": datetime(2026, 1, 1, tzinfo=UTC)},
        {"expires_at": datetime(2025, 12, 31, tzinfo=UTC)},
        {"used_at": datetime(2025, 12, 31, tzinfo=UTC)},
        {"used_at": datetime(2026, 1, 8, tzinfo=UTC)},
    ],
)
def test_database_enforces_invitation_invariants(invitation_sessions, changes):
    values = {
        "email": "friend@example.com",
        "code_hash": "a" * 64,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "expires_at": datetime(2026, 1, 8, tzinfo=UTC),
        "used_at": None,
        **changes,
    }
    with invitation_sessions() as session:
        with pytest.raises(IntegrityError):
            session.execute(RegistrationInvitation.__table__.insert().values(**values))
        session.rollback()


@pytest.fixture
def cli_database(tmp_path, monkeypatch):
    path = tmp_path / "metadata ?#% space" / "isojam.db"
    monkeypatch.setenv("ISOJAM_DATABASE_PATH", str(path))
    monkeypatch.delenv("ALEMBIC_DATABASE_URL", raising=False)
    command.upgrade(Config("alembic.ini"), "head")
    monkeypatch.delenv("ISOJAM_JWT_SECRET_KEY", raising=False)
    monkeypatch.setenv("ISOJAM_PROCESSING_MODE", "local")
    monkeypatch.setenv("ISOJAM_REGISTRATION_MODE", "closed")
    # Issuance follows the API's path, not the migration-only override.
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", "invalid-migration-only-override")
    return path


def run_cli(tmp_path, *arguments, block_inference=False):
    prefix = ["-m", "app.invitations"]
    if block_inference:
        prefix = [
            "-c",
            """
import importlib.abc
import runpy
import sys

class BlockInference(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'torch', 'bs_roformer'}:
            raise AssertionError('Invitation command must not import inference packages')

sys.meta_path.insert(0, BlockInference())
sys.argv[0] = 'app.invitations'
runpy.run_module('app.invitations', run_name='__main__')
""",
        ]
    return subprocess.run(
        [sys.executable, *prefix, *arguments],
        cwd=tmp_path,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("days", [7, 2])
def test_cli_persists_before_returning_code_without_jwt_or_inference(
    cli_database, tmp_path, days
):
    arguments = [] if days == 7 else ["--expires-in-days", str(days)]
    result = run_cli(
        tmp_path, "create", "Friend@Example.COM", *arguments, block_inference=days == 7
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert len(result.stdout.splitlines()) == 1
    output = json.loads(result.stdout)
    engine = create_engine(URL.create("sqlite", database=str(cli_database)))
    try:
        with sessionmaker(engine)() as session:
            row = session.get(RegistrationInvitation, UUID(output["id"]))
            assert row.email == output["email"] == "friend@example.com"
            assert row.code_hash == sha256(output["code"].encode()).hexdigest()
            assert row.created_at == datetime.fromisoformat(output["created_at"])
            assert row.expires_at == datetime.fromisoformat(output["expires_at"])
            assert row.expires_at - row.created_at == timedelta(days=days)
            assert row.used_at is None
            assert session.scalars(select(User)).all() == []
        assert {
            column["name"]
            for column in inspect(engine).get_columns("registration_invitations")
        } == {"id", "email", "code_hash", "created_at", "expires_at", "used_at"}
    finally:
        engine.dispose()


def test_cli_rejects_registered_email_without_issuing_a_code(cli_database, tmp_path):
    engine = create_engine(URL.create("sqlite", database=str(cli_database)))
    try:
        with sessionmaker(engine)() as session:
            create_test_user(session, email="friend@example.com")
            session.commit()
        result = run_cli(tmp_path, "create", "FRIEND@Example.COM")
        assert result.returncode == 1 and result.stdout == ""
        assert "already registered" in result.stderr
        with sessionmaker(engine)() as session:
            assert session.scalars(select(RegistrationInvitation)).all() == []
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["create", "invalid-email"],
        ["create", "friend@example.com", "--expires-in-days", "0"],
        ["create", "friend@example.com", "--expires-in-days", "31"],
        ["create", "friend@example.com", "--expires-in-days", "abc"],
    ],
)
def test_cli_rejects_bad_arguments_before_initializing_database(
    tmp_path, monkeypatch, arguments
):
    path = tmp_path / "not-created" / "isojam.db"
    monkeypatch.setenv("ISOJAM_DATABASE_PATH", str(path))
    result = run_cli(tmp_path, *arguments)
    assert result.returncode == 2 and result.stdout == ""
    assert not path.parent.exists()


def test_cli_requires_migrations_and_prints_no_code_on_database_error(
    tmp_path, monkeypatch
):
    path = tmp_path / "unmigrated.db"
    monkeypatch.setenv("ISOJAM_DATABASE_PATH", str(path))
    result = run_cli(tmp_path, "create", "friend@example.com")
    assert result.returncode == 1 and result.stdout == ""
    assert "Alembic migrations" in result.stderr


def test_cli_does_not_print_a_code_or_persist_an_invitation_when_commit_fails(
    invitation_sessions, monkeypatch, capsys
):
    monkeypatch.setattr(database, "SessionLocal", invitation_sessions)
    monkeypatch.setattr(database, "engine", invitation_sessions.kw["bind"])
    monkeypatch.setattr(
        invitations.secrets, "token_urlsafe", lambda _: "unshared-secret"
    )

    def fail_commit(session):
        raise SQLAlchemyError("synthetic commit failure")

    monkeypatch.setattr(database, "commit_session", fail_commit)
    assert invitations.main(["create", "friend@example.com"]) == 1
    output = capsys.readouterr()
    assert output.out == "" and "unshared-secret" not in output.err
    with invitation_sessions() as session:
        assert session.scalars(select(RegistrationInvitation)).all() == []
