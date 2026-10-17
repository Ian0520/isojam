from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import app.invitations as invitations
import app.routers.auth as auth_routes
from app.database import enable_sqlite_foreign_keys
from app.db_models import RegistrationInvitation, User
from app.main import create_app
from app.schemas import RegisterRequest
from app.security import decode_access_token, verify_password
from tests.factories import create_test_user

PASSWORD = "correct-horse-battery-staple"
DENIED = {"detail": "Invitation is invalid or unavailable"}


@pytest.fixture
def context(invitation_sessions, jwt_secret_key, monkeypatch):
    clock = SimpleNamespace(now=datetime(2026, 1, 1, tzinfo=UTC))
    monkeypatch.setattr(invitations, "_utc_now", lambda: clock.now)
    app = create_app(
        db_session_factory=invitation_sessions,
        jwt_secret_key=jwt_secret_key,
        processing_mode="disabled",
        registration_mode="invite",
    )
    with TestClient(app) as client:
        yield SimpleNamespace(
            sessions=invitation_sessions,
            engine=invitation_sessions.kw["bind"],
            client=client,
            clock=clock,
            secret=jwt_secret_key,
        )


def issue(context, email="friend@example.com"):
    with context.sessions() as session:
        issued = invitations.issue_invitation(session, email)
        session.commit()
        return issued


def register(context, issued, **overrides):
    return context.client.post(
        "/register",
        json={
            "email": issued.email,
            "password": PASSWORD,
            "invitation_code": issued.code,
            **overrides,
        },
    )


def assert_unused_without_user(context, issued):
    with context.sessions() as session:
        assert session.get(RegistrationInvitation, issued.id).used_at is None
        assert session.scalars(select(User)).all() == []


def test_valid_invitation_creates_hashed_account_consumes_once_and_allows_login(
    context,
):
    issued = issue(context)
    response = register(context, issued, email="FRIEND@Example.COM")
    assert response.status_code == 201
    assert set(response.json()) == {"id", "email"}
    assert issued.code not in response.text
    user_id = UUID(response.json()["id"])
    with context.sessions() as session:
        user = session.get(User, user_id)
        assert user.email == "friend@example.com"
        assert user.password_hash != PASSWORD and verify_password(
            PASSWORD, user.password_hash
        )
        assert (
            session.get(RegistrationInvitation, issued.id).used_at == context.clock.now
        )
    login = context.client.post(
        "/login", json={"email": issued.email, "password": PASSWORD}
    )
    assert login.status_code == 200
    assert decode_access_token(login.json()["access_token"], context.secret) == user_id
    replay = register(context, issued)
    assert replay.status_code == 403 and replay.json() == DENIED
    with context.sessions() as session:
        assert session.scalars(select(User.id)).all() == [user_id]


@pytest.mark.parametrize(
    "reason",
    [
        "missing",
        "null",
        "empty",
        "malformed",
        "long",
        "unknown",
        "email",
        "expired",
        "future",
        "used",
    ],
)
def test_unavailable_invitation_is_generic_and_skips_user_lookup_and_password_hashing(
    context, monkeypatch, reason
):
    issued = issue(context)
    payload = {
        "email": issued.email,
        "password": PASSWORD,
        "invitation_code": issued.code,
    }
    if reason == "missing":
        payload.pop("invitation_code")
    elif reason == "null":
        payload["invitation_code"] = None
    elif reason == "empty":
        payload["invitation_code"] = ""
    elif reason == "malformed":
        payload["invitation_code"] = "!" * 43
    elif reason == "long":
        payload["invitation_code"] = "A" * 1024
    elif reason == "unknown":
        payload["invitation_code"] = (
            "A" if issued.code[0] != "A" else "B"
        ) + issued.code[1:]
    elif reason == "email":
        payload["email"] = "other@example.com"
    elif reason == "expired":
        context.clock.now = issued.expires_at
    elif reason == "future":
        context.clock.now = issued.created_at - timedelta(microseconds=1)
    elif reason == "used":
        assert register(context, issued).status_code == 201
    with context.sessions() as session:
        used_at = session.get(RegistrationInvitation, issued.id).used_at
        users_before = session.scalars(select(User.id)).all()
    password_hash = Mock(
        side_effect=AssertionError("Invalid invitation must not hash a password")
    )
    lookup = Mock(
        side_effect=AssertionError("Invalid invitation must not look up accounts")
    )
    monkeypatch.setattr(auth_routes, "hash_password", password_hash)
    monkeypatch.setattr(auth_routes.users, "get_user_by_email", lookup)
    response = context.client.post("/register", json=payload)
    assert response.status_code == 403 and response.json() == DENIED
    assert issued.code not in response.text
    password_hash.assert_not_called()
    lookup.assert_not_called()
    with context.sessions() as session:
        assert session.get(RegistrationInvitation, issued.id).used_at == used_at
        assert session.scalars(select(User.id)).all() == users_before


@pytest.mark.parametrize("offset, expected_status", [(-1, 201), (0, 403), (1, 403)])
def test_expiration_boundary_is_exclusive(context, offset, expected_status):
    issued = issue(context)
    context.clock.now = issued.expires_at + timedelta(microseconds=offset)
    response = register(context, issued)
    assert response.status_code == expected_status
    with context.sessions() as session:
        used_at = session.get(RegistrationInvitation, issued.id).used_at
        assert used_at == (context.clock.now if expected_status == 201 else None)


def test_expiration_is_rechecked_after_password_hashing(context, monkeypatch):
    issued = issue(context)
    original = auth_routes.hash_password

    def hash_and_expire(password):
        result = original(password)
        context.clock.now = issued.expires_at
        return result

    monkeypatch.setattr(auth_routes, "hash_password", hash_and_expire)
    response = register(context, issued)
    assert response.status_code == 403 and response.json() == DENIED
    assert_unused_without_user(context, issued)


def test_password_hashing_releases_preflight_transaction_and_holds_no_write_lock(
    context, monkeypatch
):
    issued = issue(context)
    writer_engine = create_engine(context.engine.url, connect_args={"timeout": 0})
    original = auth_routes.hash_password

    def hash_with_independent_writer(password):
        with sessionmaker(writer_engine)() as session:
            create_test_user(session, email="unrelated@example.com")
            session.commit()
        return original(password)

    monkeypatch.setattr(auth_routes, "hash_password", hash_with_independent_writer)
    try:
        assert register(context, issued).status_code == 201
        with context.sessions() as session:
            assert set(session.scalars(select(User.email))) == {
                issued.email,
                "unrelated@example.com",
            }
    finally:
        writer_engine.dispose()


def test_expiration_is_checked_after_waiting_for_the_database_write_lock(context):
    issued = issue(context)
    attempted = Event()
    writer_engine = create_engine(context.engine.url)

    def observe_lock(
        connection, cursor, statement, parameters, execution_context, executemany
    ):
        if statement.strip().upper() == "BEGIN IMMEDIATE":
            attempted.set()

    event.listen(context.engine, "before_cursor_execute", observe_lock)
    try:
        with writer_engine.connect() as blocker:
            blocker.exec_driver_sql("BEGIN IMMEDIATE")
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(register, context, issued)
                try:
                    assert attempted.wait(timeout=10), (
                        "Redemption did not reach the write lock"
                    )
                    context.clock.now = issued.expires_at
                finally:
                    blocker.rollback()
                response = future.result(timeout=15)
        assert response.status_code == 403 and response.json() == DENIED
        assert_unused_without_user(context, issued)
    finally:
        event.remove(context.engine, "before_cursor_execute", observe_lock)
        writer_engine.dispose()


def test_existing_account_does_not_consume_another_valid_invitation(context):
    issued = issue(context)
    with context.sessions() as session:
        user = create_test_user(session, email=issued.email)
        user_id = user.id
        session.commit()
    response = register(context, issued)
    assert response.status_code == 403 and response.json() == DENIED
    with context.sessions() as session:
        assert session.get(RegistrationInvitation, issued.id).used_at is None
        assert session.scalars(select(User.id)).all() == [user_id]


@pytest.mark.parametrize("failure", ["integrity", "after_insert", "commit"])
def test_failed_registration_rolls_back_user_and_claim_and_allows_retry(
    context, monkeypatch, failure
):
    issued = issue(context)

    def fail_commit(connection):
        raise RuntimeError("synthetic commit failure")

    with monkeypatch.context() as patch:
        if failure == "integrity":

            def fail_insert(*args):
                raise IntegrityError(
                    "INSERT INTO users ...", None, Exception("synthetic duplicate")
                )

            patch.setattr(auth_routes.users, "create_user", fail_insert)
        elif failure == "after_insert":
            original = auth_routes.redeem_invitation

            def fail_after_insert(*args, **kwargs):
                original(*args, **kwargs)
                raise RuntimeError("synthetic error after insert")

            patch.setattr(auth_routes, "redeem_invitation", fail_after_insert)
        if failure == "commit":
            event.listen(context.engine, "commit", fail_commit)
        try:
            if failure == "integrity":
                response = register(context, issued)
                assert response.status_code == 403 and response.json() == DENIED
            else:
                with pytest.raises(RuntimeError, match="synthetic"):
                    register(context, issued)
        finally:
            if failure == "commit":
                event.remove(context.engine, "commit", fail_commit)
    assert_unused_without_user(context, issued)
    assert register(context, issued).status_code == 201


@pytest.mark.parametrize("different_invitations", [False, True])
def test_competing_api_instances_create_one_account_and_consume_one_invitation(
    context, monkeypatch, different_invitations
):
    first = issue(context)
    second = issue(context) if different_invitations else first
    barrier = Barrier(2)
    original = auth_routes.hash_password

    def synchronize_hash(password):
        value = original(password)
        barrier.wait(timeout=10)
        return value

    monkeypatch.setattr(auth_routes, "hash_password", synchronize_hash)
    other_engine = create_engine(context.engine.url)
    enable_sqlite_foreign_keys(other_engine)
    other_app = create_app(
        db_session_factory=sessionmaker(other_engine),
        jwt_secret_key=context.secret,
        processing_mode="disabled",
        registration_mode="invite",
    )
    try:
        with (
            TestClient(other_app) as other_client,
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            requests = [
                pool.submit(register, context, first),
                pool.submit(
                    other_client.post,
                    "/register",
                    json={
                        "email": second.email,
                        "password": PASSWORD,
                        "invitation_code": second.code,
                    },
                ),
            ]
            responses = [future.result(timeout=15) for future in requests]
        assert sorted(response.status_code for response in responses) == [201, 403]
        assert (
            next(
                response for response in responses if response.status_code == 403
            ).json()
            == DENIED
        )
        with context.sessions() as session:
            assert len(session.scalars(select(User)).all()) == 1
            rows = session.scalars(select(RegistrationInvitation)).all()
            assert len(rows) == (2 if different_invitations else 1)
            assert sum(row.used_at is not None for row in rows) == 1
    finally:
        other_engine.dispose()


@pytest.mark.parametrize("mode", ["open", "closed"])
def test_existing_modes_do_not_consume_a_supplied_invitation(context, mode):
    issued = issue(context)
    app = create_app(
        db_session_factory=context.sessions,
        jwt_secret_key=context.secret,
        processing_mode="disabled",
        registration_mode=mode,
    )
    with TestClient(app) as client:
        response = client.post(
            "/register",
            json={
                "email": issued.email,
                "password": PASSWORD,
                "invitation_code": issued.code,
            },
        )
        assert response.status_code == (201 if mode == "open" else 403)
    with context.sessions() as session:
        assert session.get(RegistrationInvitation, issued.id).used_at is None
        assert len(session.scalars(select(User)).all()) == (1 if mode == "open" else 0)


def test_invalid_password_does_not_consume_invitation(context):
    issued = issue(context)
    assert register(context, issued, password="").status_code == 422
    assert_unused_without_user(context, issued)


def test_request_representation_hides_invitation_secret(context):
    issued = issue(context)
    body = RegisterRequest(
        email=issued.email, password=PASSWORD, invitation_code=issued.code
    )
    assert issued.code not in repr(body)


def test_redemption_requires_a_fresh_transaction(context):
    issued = issue(context)
    with context.sessions() as session:
        session.execute(select(User.id))
        with pytest.raises(RuntimeError, match="fresh transaction"):
            invitations.redeem_invitation(
                session,
                email=issued.email,
                code_hash=invitations.hash_invitation_code(issued.code),
                password_hash="hash",
            )
    assert_unused_without_user(context, issued)
