from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, timedelta
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import URL, create_engine, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import app.routers.jobs as job_routes
from app.database import enable_sqlite_foreign_keys
from app.db_models import Base, Job, JobOutput, JobSubmissionReceipt, Upload
from app.main import create_app
from app.repositories import job_submissions
from app.security import create_access_token
from tests.factories import create_test_job, create_test_upload, create_test_user


@pytest.fixture(params=["models", "migration"])
def submission_session_factory(request, tmp_path, monkeypatch):
    url = URL.create("sqlite", database=str(tmp_path / "submissions.db"))
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


@pytest.fixture
def submission_context(
    submission_session_factory, fake_model_session, jwt_secret_key, monkeypatch
):
    factory = submission_session_factory
    with factory() as session:
        user = create_test_user(session)
        other_user = create_test_user(session, "other@example.com")
        upload = create_test_upload(session, user)
        other_owned_upload = create_test_upload(session, user)
        foreign_upload = create_test_upload(session, other_user)
        ids = (
            user.id,
            other_user.id,
            upload.id,
            other_owned_upload.id,
            foreign_upload.id,
        )
        session.commit()
    user_id, other_user_id, upload_id, other_owned_id, foreign_id = ids

    def headers_for(owner):
        token = create_access_token(
            user_id=owner,
            secret_key=jwt_secret_key,
            expires_delta=timedelta(minutes=5),
        )
        return {"Authorization": f"Bearer {token}", "Idempotency-Key": "request-1"}

    process = Mock()
    monkeypatch.setattr(job_routes, "process_job", process)
    app = create_app(
        model_session_factory=lambda: fake_model_session,
        db_session_factory=factory,
        jwt_secret_key=jwt_secret_key,
        processing_mode="local",
        max_unfinished_jobs_per_user=2,
    )
    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client,
            app=app,
            factory=factory,
            process=process,
            user_id=user_id,
            upload_id=upload_id,
            other_owned_id=other_owned_id,
            foreign_id=foreign_id,
            headers=headers_for(user_id),
            foreign_headers=headers_for(other_user_id),
        )


def submit(context, *, upload_id=None, headers=None):
    return context.client.post(
        "/jobs",
        json={"upload_id": str(upload_id or context.upload_id)},
        headers=context.headers if headers is None else headers,
    )


def counts(context):
    with context.factory() as session:
        return (
            session.scalar(select(func.count()).select_from(Job)),
            session.scalar(select(func.count()).select_from(JobSubmissionReceipt)),
        )


def test_retry_returns_original_job_without_dispatch(submission_context):
    context = submission_context
    first, replay = submit(context), submit(context)
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert counts(context) == (1, 1)
    context.process.assert_called_once()
    with context.factory() as session:
        receipt = session.get(JobSubmissionReceipt, (context.user_id, "request-1"))
        assert receipt.job_id == UUID(first.json()["id"])
        assert receipt.created_at.tzinfo is UTC


def test_retry_uses_canonical_upload_identity(submission_context):
    context = submission_context
    first = submit(context)
    replay = context.client.post(
        "/jobs",
        json={"upload_id": str(context.upload_id).upper()},
        headers=context.headers,
    )
    assert first.status_code == replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]
    context.process.assert_called_once()


@pytest.mark.parametrize("status", ["processing", "completed", "failed"])
def test_retry_returns_current_status_and_outputs(submission_context, status):
    context = submission_context
    first = submit(context)
    job_id = UUID(first.json()["id"])
    with context.factory() as session:
        session.get(Job, job_id).status = status
        if status == "completed":
            session.add(
                JobOutput(job_id=job_id, stem="guitar", path="/unused/guitar.wav")
            )
        session.commit()
    replay = submit(context)
    assert replay.status_code == 200
    assert replay.json()["id"] == str(job_id)
    assert replay.json()["status"] == status
    assert replay.json()["outputs"] == (
        {"guitar": f"/jobs/{job_id}/outputs/guitar"} if status == "completed" else {}
    )
    assert counts(context) == (1, 1)
    context.process.assert_called_once()


def test_retry_works_when_quota_is_full(submission_context):
    context = submission_context
    first = submit(context)
    assert (
        submit(
            context, headers={**context.headers, "Idempotency-Key": "request-2"}
        ).status_code
        == 200
    )
    assert (
        submit(
            context, headers={**context.headers, "Idempotency-Key": "request-3"}
        ).status_code
        == 429
    )
    replay = submit(context)
    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]
    assert counts(context) == (2, 2)
    assert context.process.call_count == 2


def test_quota_rejection_does_not_consume_key(submission_context):
    context = submission_context
    first = submit(context)
    assert (
        submit(
            context, headers={**context.headers, "Idempotency-Key": "request-2"}
        ).status_code
        == 200
    )
    headers = {**context.headers, "Idempotency-Key": "request-3"}
    assert submit(context, headers=headers).status_code == 429
    with context.factory() as session:
        assert session.get(JobSubmissionReceipt, (context.user_id, "request-3")) is None
        session.get(Job, UUID(first.json()["id"])).status = "completed"
        session.commit()
    assert submit(context, headers=headers).status_code == 200
    assert counts(context) == (3, 3)
    assert context.process.call_count == 3


def test_reusing_key_for_different_request_returns_conflict(submission_context):
    context = submission_context
    assert submit(context).status_code == 200
    conflict = submit(context, upload_id=context.other_owned_id)
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "Idempotency key belongs to another request"
    assert counts(context) == (1, 1)
    context.process.assert_called_once()


def test_keys_are_scoped_to_users(submission_context):
    context = submission_context
    first = submit(context)
    other = submit(
        context, upload_id=context.foreign_id, headers=context.foreign_headers
    )
    assert first.status_code == other.status_code == 200
    assert first.json()["id"] != other.json()["id"]
    assert (
        submit(
            context, upload_id=context.foreign_id, headers=context.foreign_headers
        ).json()["id"]
        == other.json()["id"]
    )
    assert counts(context) == (2, 2)
    assert context.process.call_count == 2


@pytest.mark.parametrize("key", [None, "another-key", "REQUEST-1"])
def test_new_or_absent_key_allows_another_job(submission_context, key):
    context = submission_context
    first = submit(context)
    headers = {"Authorization": context.headers["Authorization"]}
    if key is not None:
        headers["Idempotency-Key"] = key
    second = submit(context, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] != second.json()["id"]
    assert counts(context) == (2, 1 if key is None else 2)
    assert context.process.call_count == 2


def test_unkeyed_requests_preserve_existing_behavior(submission_context):
    context = submission_context
    headers = {"Authorization": context.headers["Authorization"]}
    first, second = submit(context, headers=headers), submit(context, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] != second.json()["id"]
    assert counts(context) == (2, 0)
    assert context.process.call_count == 2


@pytest.mark.parametrize("key", ["", " ", "has space", "bad/key", "a" * 129])
def test_invalid_key_is_rejected_without_creating_job(submission_context, key):
    context = submission_context
    response = submit(context, headers={**context.headers, "Idempotency-Key": key})
    assert response.status_code == 422
    assert counts(context) == (0, 0)
    context.process.assert_not_called()


@pytest.mark.parametrize("key", ["a", "A_b.c:d-9" + "x" * 119])
def test_valid_key_boundaries_are_accepted(submission_context, key):
    context = submission_context
    headers = {**context.headers, "Idempotency-Key": key}
    first, replay = submit(context, headers=headers), submit(context, headers=headers)
    assert first.status_code == replay.status_code == 200
    assert first.json()["id"] == replay.json()["id"]
    context.process.assert_called_once()


def test_ownership_and_authentication_are_checked_before_replay(submission_context):
    context = submission_context
    assert submit(context).status_code == 200
    for upload_id in (context.foreign_id, uuid4()):
        assert submit(context, upload_id=upload_id).status_code == 404
    assert submit(context, headers={"Idempotency-Key": "request-1"}).status_code == 401
    assert counts(context) == (1, 1)
    context.process.assert_called_once()


def test_replay_survives_api_restart_in_disabled_mode(
    submission_context, jwt_secret_key
):
    context = submission_context
    first = submit(context)
    model_factory = Mock(side_effect=AssertionError("Must not load model"))
    restarted_app = create_app(
        db_session_factory=context.factory,
        model_session_factory=model_factory,
        jwt_secret_key=jwt_secret_key,
        processing_mode="disabled",
    )
    with TestClient(restarted_app) as restarted:
        replay = restarted.post(
            "/jobs", json={"upload_id": str(context.upload_id)}, headers=context.headers
        )
        new = restarted.post(
            "/jobs",
            json={"upload_id": str(context.upload_id)},
            headers={**context.headers, "Idempotency-Key": "new-request"},
        )
    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]
    assert new.status_code == 503
    assert counts(context) == (1, 1)
    context.process.assert_called_once()
    model_factory.assert_not_called()


def test_receipt_failure_rolls_back_job_and_allows_retry(submission_context):
    context = submission_context
    engine = context.factory.kw["bind"]

    def fail_receipt(
        connection, cursor, statement, parameters, execution_context, executemany
    ):
        if statement.lstrip().upper().startswith("INSERT INTO JOB_SUBMISSION_RECEIPTS"):
            raise RuntimeError("receipt storage failed")

    event.listen(engine, "before_cursor_execute", fail_receipt)
    try:
        with pytest.raises(RuntimeError, match="receipt storage failed"):
            submit(context)
    finally:
        event.remove(engine, "before_cursor_execute", fail_receipt)
    assert counts(context) == (0, 0)
    context.process.assert_not_called()
    assert submit(context).status_code == 200
    assert counts(context) == (1, 1)
    context.process.assert_called_once()


@pytest.mark.parametrize("conflicting_uploads", [False, True])
def test_simultaneous_retries_converge_at_quota_boundary(
    submission_context, conflicting_uploads
):
    context = submission_context
    with context.factory() as session:
        upload = session.get(Upload, context.upload_id)
        create_test_job(session, upload, "processing")
        session.commit()
    engine = context.factory.kw["bind"]
    barrier = Barrier(2)

    def synchronize_inserts(
        connection, cursor, statement, parameters, execution_context, executemany
    ):
        if statement.lstrip().upper().startswith("INSERT INTO JOBS"):
            barrier.wait(timeout=10)

    event.listen(engine, "before_cursor_execute", synchronize_inserts)
    upload_ids = [
        context.upload_id,
        context.other_owned_id if conflicting_uploads else context.upload_id,
    ]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(
                pool.map(
                    lambda upload_id: submit(context, upload_id=upload_id), upload_ids
                )
            )
    finally:
        event.remove(engine, "before_cursor_execute", synchronize_inserts)
    assert sorted(response.status_code for response in responses) == (
        [200, 409] if conflicting_uploads else [200, 200]
    )
    if not conflicting_uploads:
        assert responses[0].json()["id"] == responses[1].json()["id"]
    assert counts(context) == (2, 1)
    context.process.assert_called_once()


def test_repository_does_not_commit_job_or_receipt(submission_context):
    context = submission_context
    with context.factory() as session:
        result = job_submissions.create_submission(
            session,
            context.upload_id,
            context.user_id,
            key="uncommitted",
            max_unfinished_jobs=2,
        )
        assert result.created
        with context.factory() as observer:
            assert observer.scalar(select(func.count()).select_from(Job)) == 0
            assert (
                observer.scalar(select(func.count()).select_from(JobSubmissionReceipt))
                == 0
            )
        session.rollback()
    assert counts(context) == (0, 0)


def test_receipt_primary_key_and_foreign_keys_are_enforced(submission_context):
    context = submission_context
    first = submit(context)
    job_id = UUID(first.json()["id"])
    for user_id, key, target_job_id in [
        (context.user_id, "request-1", job_id),
        (uuid4(), "missing-user", job_id),
        (context.user_id, "missing-job", uuid4()),
    ]:
        with context.factory() as session:
            session.add(
                JobSubmissionReceipt(
                    user_id=user_id,
                    key=key,
                    job_id=target_job_id,
                    request_fingerprint="a" * 64,
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()
    assert counts(context) == (1, 1)
