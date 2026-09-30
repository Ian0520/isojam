from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

import app.repositories.jobs as jobs
import app.routers.jobs as job_routes
from app.db_models import Job, Upload, User
from app.main import create_app
from tests.factories import create_test_job, create_test_upload, create_test_user


@pytest.fixture
def job_limit_client(
    fake_model_session, test_session_factory, jwt_secret_key, monkeypatch
):
    process_job = Mock()
    monkeypatch.setattr(job_routes, "process_job", process_job)
    test_app = create_app(
        model_session_factory=lambda: fake_model_session,
        db_session_factory=test_session_factory,
        jwt_secret_key=jwt_secret_key,
        max_unfinished_jobs_per_user=2,
    )
    with TestClient(test_app) as client:
        yield client, process_job


def seed_owned_jobs(test_session_factory, statuses):
    with test_session_factory() as session:
        # auth_headers creates the authenticated user before this helper runs.
        user = session.scalar(select(User))
        upload = create_test_upload(session, user)
        upload_id = upload.id
        for status in statuses:
            create_test_job(session, upload, status)
        session.commit()
    return upload_id


@pytest.mark.parametrize(
    "statuses",
    [("pending", "pending"), ("pending", "processing"), ("processing", "processing")],
)
def test_create_job_rejects_full_unfinished_job_allowance(
    job_limit_client, auth_headers, test_session_factory, statuses
):
    client, process_job = job_limit_client
    upload_id = seed_owned_jobs(test_session_factory, statuses)
    response = client.post(
        "/jobs", json={"upload_id": str(upload_id)}, headers=auth_headers
    )
    assert response.status_code == 429
    assert (
        response.json()["detail"]
        == "Unfinished job limit reached; wait for a job to finish"
    )
    process_job.assert_not_called()
    with test_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 2


@pytest.mark.parametrize("finished_status", ["completed", "failed"])
def test_finishing_a_job_frees_an_admission_slot(
    job_limit_client, auth_headers, test_session_factory, finished_status
):
    client, process_job = job_limit_client
    upload_id = seed_owned_jobs(test_session_factory, ["pending", "processing"])
    rejected = client.post(
        "/jobs", json={"upload_id": str(upload_id)}, headers=auth_headers
    )
    assert rejected.status_code == 429
    with test_session_factory() as session:
        job = session.scalar(select(Job).where(Job.status == "processing"))
        jobs.update_job_status(session, job.id, finished_status)
        session.commit()
    accepted = client.post(
        "/jobs", json={"upload_id": str(upload_id)}, headers=auth_headers
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "pending"
    process_job.assert_called_once()
    with test_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 3


def test_unfinished_job_limit_counts_across_owned_uploads(
    job_limit_client, auth_headers, test_session_factory
):
    client, process_job = job_limit_client
    upload_id = seed_owned_jobs(test_session_factory, ["pending"])
    with test_session_factory() as session:
        original_upload = session.get(Upload, upload_id)
        user = session.get(User, original_upload.user_id)
        other_upload = create_test_upload(session, user)
        create_test_job(session, other_upload, "processing")
        session.commit()
    response = client.post(
        "/jobs", json={"upload_id": str(upload_id)}, headers=auth_headers
    )
    assert response.status_code == 429
    process_job.assert_not_called()


def test_another_users_jobs_do_not_use_the_allowance(
    job_limit_client, auth_headers, test_session_factory
):
    client, process_job = job_limit_client
    upload_id = seed_owned_jobs(test_session_factory, ["pending"])
    with test_session_factory() as session:
        other_user = create_test_user(session, "other@example.com")
        other_upload = create_test_upload(session, other_user)
        for _ in range(3):
            create_test_job(session, other_upload, "processing")
        session.commit()
    response = client.post(
        "/jobs", json={"upload_id": str(upload_id)}, headers=auth_headers
    )
    assert response.status_code == 200
    process_job.assert_called_once()


def test_simultaneous_submissions_cannot_exceed_the_allowance(
    job_limit_client, auth_headers, test_session_factory, test_engine
):
    client, process_job = job_limit_client
    upload_id = seed_owned_jobs(test_session_factory, ["pending"])
    admission_barrier = Barrier(2)

    def synchronize_inserts(
        connection, cursor, statement, parameters, context, executemany
    ):
        if statement.lstrip().upper().startswith("INSERT INTO JOBS"):
            # Both requests must reach their INSERT before either executes it.
            # A separate count-then-insert implementation would exceed the limit.
            admission_barrier.wait(timeout=10)

    event.listen(test_engine, "before_cursor_execute", synchronize_inserts)

    def submit():
        return client.post(
            "/jobs", json={"upload_id": str(upload_id)}, headers=auth_headers
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda _: submit(), range(2)))
    finally:
        event.remove(test_engine, "before_cursor_execute", synchronize_inserts)
    assert sorted(response.status_code for response in responses) == [200, 429]
    process_job.assert_called_once()
    with test_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 2


def test_missing_or_foreign_upload_still_returns_404_when_allowance_is_full(
    job_limit_client,
    auth_headers,
    test_session_factory,
):
    client, process_job = job_limit_client
    seed_owned_jobs(test_session_factory, ["pending", "processing"])
    with test_session_factory() as session:
        other_user = create_test_user(session, "other@example.com")
        foreign_upload = create_test_upload(session, other_user)
        foreign_upload_id = foreign_upload.id
        session.commit()
    for upload_id in (uuid4(), foreign_upload_id):
        response = client.post(
            "/jobs", json={"upload_id": str(upload_id)}, headers=auth_headers
        )
        assert response.status_code == 404
    process_job.assert_not_called()
    with test_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 2


@pytest.mark.parametrize("limit", [1, 3])
def test_job_admission_enforces_configured_allowance(
    fake_model_session,
    test_session_factory,
    jwt_secret_key,
    monkeypatch,
    auth_headers,
    limit,
):
    process_job = Mock()
    monkeypatch.setattr(job_routes, "process_job", process_job)
    upload_id = seed_owned_jobs(test_session_factory, [])
    test_app = create_app(
        model_session_factory=lambda: fake_model_session,
        db_session_factory=test_session_factory,
        jwt_secret_key=jwt_secret_key,
        max_unfinished_jobs_per_user=limit,
    )
    with TestClient(test_app) as client:
        for _ in range(limit):
            response = client.post(
                "/jobs", json={"upload_id": str(upload_id)}, headers=auth_headers
            )
            assert response.status_code == 200
        response = client.post(
            "/jobs", json={"upload_id": str(upload_id)}, headers=auth_headers
        )
        assert response.status_code == 429
    assert process_job.call_count == limit
    with test_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == limit
