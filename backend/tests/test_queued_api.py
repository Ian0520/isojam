from contextlib import ExitStack
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

import app.routers.jobs as job_routes
import app.storage as storage
from app.database import enable_sqlite_foreign_keys
from app.db_models import Job, JobAttempt, JobOutput, JobSubmissionReceipt
from app.main import create_app
from app.repositories import job_reservations, jobs
from app.security import create_access_token
from tests.factories import create_test_user


@pytest.fixture
def queued_context(reservation_engine, jwt_secret_key, monkeypatch, tmp_path):
    factory = sessionmaker(reservation_engine)
    with factory() as session:
        user = create_test_user(session)
        user_id = user.id
        session.commit()
    token = create_access_token(
        user_id=user_id,
        secret_key=jwt_secret_key,
        expires_delta=timedelta(minutes=5),
    )
    model_factory = Mock(side_effect=AssertionError("Queued API must not load a model"))
    process = Mock()
    monkeypatch.setattr(job_routes, "process_job", process)
    monkeypatch.setattr(storage, "UPLOAD_DIR", tmp_path / "uploads")
    app = create_app(
        model_session_factory=model_factory,
        db_session_factory=factory,
        jwt_secret_key=jwt_secret_key,
        processing_mode="queued",
    )
    with ExitStack() as stack:
        client = stack.enter_context(TestClient(app))
        yield SimpleNamespace(
            stop_api=stack.close,
            client=client,
            app=app,
            factory=factory,
            engine=reservation_engine,
            headers={"Authorization": f"Bearer {token}"},
            user_id=user_id,
            model_factory=model_factory,
            process=process,
        )
    model_factory.assert_not_called()
    process.assert_not_called()


def upload(context, wav_bytes):
    response = context.client.post(
        "/uploads",
        files={"audio_file": ("song.wav", wav_bytes, "audio/wav")},
        headers=context.headers,
    )
    assert response.status_code == 200
    return response.json()["id"]


def test_committed_queued_upload_job_is_discoverable_after_api_and_engine_restart(
    queued_context,
    wav_bytes,
    jwt_secret_key,
):
    context = queued_context
    upload_id = upload(context, wav_bytes)
    headers = {**context.headers, "Idempotency-Key": "durable-request"}
    response = context.client.post(
        "/jobs", json={"upload_id": upload_id}, headers=headers
    )
    assert response.status_code == 200
    body = response.json()
    job_id = UUID(body["id"])
    assert body == {
        "id": str(job_id),
        "upload_id": upload_id,
        "status": "pending",
        "outputs": {},
    }
    with context.factory() as session:
        job = session.get(Job, job_id)
        assert job.execution_backend == "queued"
        assert job.status == "pending"
        assert (
            session.get(
                JobSubmissionReceipt, (context.user_id, "durable-request")
            ).job_id
            == job_id
        )
        assert session.scalar(select(func.count()).select_from(JobAttempt)) == 0
        assert session.scalar(select(func.count()).select_from(JobOutput)) == 0
    # Explicitly shut down the accepting API before opening a fresh engine/app.
    context.stop_api()
    restarted_engine = create_engine(context.engine.url)
    enable_sqlite_foreign_keys(restarted_engine)
    try:
        restarted_app = create_app(
            model_session_factory=context.model_factory,
            db_session_factory=sessionmaker(restarted_engine),
            jwt_secret_key=jwt_secret_key,
            processing_mode="queued",
        )
        with TestClient(restarted_app) as restarted:
            assert restarted.get(f"/jobs/{job_id}", headers=headers).json() == body
            replay = restarted.post(
                "/jobs", json={"upload_id": upload_id}, headers=headers
            )
            assert replay.status_code == 200
            assert replay.json() == body
            assert (
                restarted.get(
                    f"/jobs/{job_id}/outputs/guitar", headers=headers
                ).status_code
                == 409
            )
        reservation = job_reservations.reserve_next_job(
            restarted_engine, dispatcher_id=uuid4()
        )
        assert reservation is not None
        assert reservation.job_id == job_id
        with sessionmaker(restarted_engine)() as session:
            assert session.scalar(select(func.count()).select_from(Job)) == 1
            assert (
                session.scalar(select(func.count()).select_from(JobSubmissionReceipt))
                == 1
            )
            assert session.get(Job, job_id).status == "pending"
            assert session.get(JobAttempt, reservation.attempt_id).phase == "reserved"
    finally:
        restarted_engine.dispose()


def test_failed_acceptance_commit_leaves_no_job_or_receipt_and_retry_can_succeed(
    queued_context,
    wav_bytes,
):
    context = queued_context
    upload_id = upload(context, wav_bytes)
    headers = {**context.headers, "Idempotency-Key": "retry-after-commit-failure"}

    def fail_commit(connection):
        raise RuntimeError("acceptance commit failed")

    event.listen(context.engine, "commit", fail_commit)
    try:
        with pytest.raises(RuntimeError, match="acceptance commit failed"):
            context.client.post("/jobs", json={"upload_id": upload_id}, headers=headers)
    finally:
        event.remove(context.engine, "commit", fail_commit)
    with context.factory() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 0
        assert (
            session.scalar(select(func.count()).select_from(JobSubmissionReceipt)) == 0
        )
    response = context.client.post(
        "/jobs", json={"upload_id": upload_id}, headers=headers
    )
    assert response.status_code == 200
    with context.factory() as session:
        assert (
            session.get(Job, UUID(response.json()["id"])).execution_backend == "queued"
        )
        assert session.scalar(select(func.count()).select_from(Job)) == 1
        assert (
            session.scalar(select(func.count()).select_from(JobSubmissionReceipt)) == 1
        )


def test_admission_rejects_invalid_execution_backend_before_creating_job(
    queued_context, wav_bytes
):
    context = queued_context
    upload_id = UUID(upload(context, wav_bytes))
    with context.factory() as session:
        with pytest.raises(ValueError, match="execution_backend"):
            jobs.create_job_with_limit(
                session,
                upload_id,
                context.user_id,
                max_unfinished_jobs=2,
                execution_backend="disabled",
            )
        assert session.scalar(select(func.count()).select_from(Job)) == 0


def test_reader_blocked_commit_cannot_leak_job_or_receipt_into_next_request(
    queued_context,
    wav_bytes,
):
    context = queued_context
    upload_id = upload(context, wav_bytes)
    headers = {**context.headers, "Idempotency-Key": "busy-commit"}
    with context.engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA busy_timeout = 25")
    observer_engine = create_engine(context.engine.url)
    try:
        with observer_engine.connect() as reader:
            reader.exec_driver_sql("BEGIN")
            reader.exec_driver_sql("SELECT id FROM jobs").all()
            with pytest.raises(OperationalError, match="locked"):
                context.client.post(
                    "/jobs", json={"upload_id": upload_id}, headers=headers
                )
            reader.rollback()
        with context.engine.connect() as connection:
            assert not connection.connection.dbapi_connection.in_transaction
        # A fresh engine and the reused pool both see no partially accepted work.
        for factory in (sessionmaker(observer_engine), context.factory):
            with factory() as session:
                assert session.scalar(select(func.count()).select_from(Job)) == 0
                assert (
                    session.scalar(
                        select(func.count()).select_from(JobSubmissionReceipt)
                    )
                    == 0
                )
        response = context.client.post(
            "/jobs", json={"upload_id": upload_id}, headers=headers
        )
        assert response.status_code == 200
        with context.factory() as session:
            assert session.scalar(select(func.count()).select_from(Job)) == 1
            assert (
                session.scalar(select(func.count()).select_from(JobSubmissionReceipt))
                == 1
            )
    finally:
        observer_engine.dispose()
