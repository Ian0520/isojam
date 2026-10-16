import base64
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.db_models import Job, User
from app.security import create_access_token
from tests.factories import create_test_upload, create_test_user

BASE_TIME = datetime(2026, 10, 16, 8, tzinfo=UTC)


@pytest.fixture
def history_account(test_session_factory, jwt_secret_key):
    with test_session_factory() as session:
        user = create_test_user(session)
        user_id = user.id
        session.commit()
    token = create_access_token(
        user_id=user_id,
        secret_key=jwt_secret_key,
        expires_delta=timedelta(minutes=5),
    )
    return SimpleNamespace(
        user_id=user_id, headers={"Authorization": f"Bearer {token}"}
    )


def add_history_job(
    session,
    user_id,
    *,
    created_at=BASE_TIME,
    job_id=None,
    filename="song.wav",
    status="completed",
    backend="local",
):
    user = session.get(User, user_id)
    upload = create_test_upload(
        session, user, original_filename=filename, stored_filename=f"{uuid4()}.wav"
    )
    job = Job(
        id=job_id or uuid4(),
        upload_id=upload.id,
        status=status,
        execution_backend=backend,
        created_at=created_at,
    )
    session.add(job)
    session.flush()
    return job


def raw_cursor(payload):
    return (
        base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )


def test_job_history_requires_authentication(client):
    response = client.get("/jobs")

    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"


def test_job_history_returns_empty_page(client, history_account):
    response = client.get("/jobs", headers=history_account.headers)

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


def test_job_history_returns_newest_first_summaries(
    client, history_account, test_session_factory, fake_model_session
):
    expected = []
    with test_session_factory() as session:
        for index, status in enumerate(
            ["pending", "processing", "completed", "failed"]
        ):
            created_at = BASE_TIME + timedelta(hours=index)
            job = add_history_job(
                session,
                history_account.user_id,
                job_id=UUID(int=100 - index),
                created_at=created_at,
                filename=f"練習-{index}.wav",
                status=status,
                backend="queued" if index % 2 else "local",
            )
            expected.append(
                {
                    "id": str(job.id),
                    "upload_id": str(job.upload_id),
                    "filename": f"練習-{index}.wav",
                    "status": status,
                    "created_at": created_at.isoformat().replace("+00:00", "Z"),
                }
            )
        session.commit()

    response = client.get("/jobs", headers=history_account.headers)

    assert response.status_code == 200
    assert response.json() == {"items": expected[::-1], "next_cursor": None}
    assert fake_model_session.called is False


def test_job_history_filters_ownership_before_page_limit(
    client, history_account, test_session_factory
):
    with test_session_factory() as session:
        other = create_test_user(session, "other@example.com")
        for index in range(3):
            add_history_job(
                session, other.id, created_at=BASE_TIME + timedelta(days=1, hours=index)
            )
        own_ids = [
            str(
                add_history_job(
                    session,
                    history_account.user_id,
                    created_at=BASE_TIME + timedelta(hours=index),
                ).id
            )
            for index in range(2)
        ]
        session.commit()

    response = client.get("/jobs", params={"limit": 2}, headers=history_account.headers)

    assert response.status_code == 200
    data = response.json()
    assert [item["id"] for item in data["items"]] == own_ids[::-1]
    assert data["next_cursor"] is None


@pytest.mark.parametrize(("limit", "expected_size"), [(None, 20), (1, 1), (100, 100)])
def test_job_history_bounds_page_size(
    client, history_account, test_session_factory, limit, expected_size
):
    with test_session_factory() as session:
        for index in range(101):
            add_history_job(
                session,
                history_account.user_id,
                created_at=BASE_TIME + timedelta(seconds=index),
            )
        session.commit()

    response = client.get(
        "/jobs",
        params={} if limit is None else {"limit": limit},
        headers=history_account.headers,
    )

    assert response.status_code == 200
    assert len(response.json()["items"]) == expected_size
    assert isinstance(response.json()["next_cursor"], str)


@pytest.mark.parametrize("limit", ["0", "-1", "101", "1.5", "abc", ""])
def test_job_history_rejects_invalid_page_size(client, history_account, limit):
    response = client.get(
        "/jobs", params={"limit": limit}, headers=history_account.headers
    )

    assert response.status_code == 422


def test_job_history_paginates_equal_timestamps_without_repeats_or_omissions(
    client, history_account, test_session_factory
):
    with test_session_factory() as session:
        for number, hours in [(7, 0), (3, 1), (9, 1), (5, 1), (1, 2)]:
            add_history_job(
                session,
                history_account.user_id,
                job_id=UUID(int=number),
                created_at=BASE_TIME + timedelta(hours=hours),
            )
        session.commit()

    params = {"limit": 2}
    seen_ids = []
    for expected_size in [2, 2, 1]:
        response = client.get("/jobs", params=params, headers=history_account.headers)
        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == expected_size
        seen_ids.extend(item["id"] for item in data["items"])
        params["cursor"] = data["next_cursor"]

    assert seen_ids == [str(UUID(int=number)) for number in [1, 9, 5, 3, 7]]
    assert data["next_cursor"] is None


def test_job_history_cursor_stays_stable_when_a_new_job_arrives(
    client, history_account, test_session_factory
):
    with test_session_factory() as session:
        ids = [
            str(
                add_history_job(
                    session,
                    history_account.user_id,
                    created_at=BASE_TIME + timedelta(hours=index),
                ).id
            )
            for index in range(3)
        ]
        session.commit()

    first = client.get("/jobs", params={"limit": 2}, headers=history_account.headers)
    assert first.status_code == 200
    assert [item["id"] for item in first.json()["items"]] == ids[:0:-1]

    with test_session_factory() as session:
        new_id = str(
            add_history_job(
                session,
                history_account.user_id,
                created_at=BASE_TIME + timedelta(hours=3),
            ).id
        )
        session.commit()

    second = client.get(
        "/jobs",
        params={"limit": 2, "cursor": first.json()["next_cursor"]},
        headers=history_account.headers,
    )
    assert second.status_code == 200
    assert [item["id"] for item in second.json()["items"]] == [ids[0]]
    assert second.json()["next_cursor"] is None

    refreshed = client.get(
        "/jobs", params={"limit": 2}, headers=history_account.headers
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["items"][0]["id"] == new_id


def test_job_history_cursor_does_not_grant_access_to_another_account(
    client, history_account, test_session_factory, jwt_secret_key
):
    with test_session_factory() as session:
        other = create_test_user(session, "other@example.com")
        other_id = other.id
        for hours in [1, 2]:
            add_history_job(
                session, other_id, created_at=BASE_TIME + timedelta(hours=hours)
            )
        own_id = str(add_history_job(session, history_account.user_id).id)
        session.commit()
    token = create_access_token(
        user_id=other_id,
        secret_key=jwt_secret_key,
        expires_delta=timedelta(minutes=5),
    )
    other_page = client.get(
        "/jobs", params={"limit": 1}, headers={"Authorization": f"Bearer {token}"}
    )
    assert other_page.status_code == 200
    cursor = other_page.json()["next_cursor"]
    assert cursor is not None

    response = client.get(
        "/jobs", params={"cursor": cursor}, headers=history_account.headers
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [own_id]
    assert response.json()["next_cursor"] is None


@pytest.mark.parametrize(
    "cursor",
    [
        pytest.param("", id="empty"),
        pytest.param("not-base64!", id="invalid-base64"),
        pytest.param("a", id="invalid-length"),
        pytest.param("a" * 513, id="oversized"),
        pytest.param(base64.urlsafe_b64encode(b"not json").decode(), id="invalid-json"),
        pytest.param(base64.urlsafe_b64encode(b"\xff").decode(), id="invalid-utf8"),
        pytest.param(raw_cursor([]), id="not-an-object"),
        pytest.param(raw_cursor({}), id="missing-fields"),
        pytest.param(
            raw_cursor({"created_at": BASE_TIME.isoformat(), "id": "invalid"}),
            id="invalid-uuid",
        ),
        pytest.param(
            raw_cursor({"created_at": "not-a-date", "id": str(UUID(int=1))}),
            id="invalid-date",
        ),
        pytest.param(
            raw_cursor({"created_at": "2026-10-16T08:00:00", "id": str(UUID(int=1))}),
            id="naive-timestamp",
        ),
        pytest.param(
            raw_cursor(
                {"created_at": "0001-01-01T00:00:00+01:00", "id": str(UUID(int=1))}
            ),
            id="utc-underflow",
        ),
        pytest.param(
            raw_cursor(
                {"created_at": "9999-12-31T23:59:59-01:00", "id": str(UUID(int=1))}
            ),
            id="utc-overflow",
        ),
        pytest.param(
            raw_cursor(
                {
                    "created_at": BASE_TIME.isoformat(),
                    "id": str(UUID(int=1)),
                    "unexpected": True,
                }
            ),
            id="unexpected-fields",
        ),
    ],
)
def test_job_history_rejects_invalid_cursor(client, history_account, cursor):
    response = client.get(
        "/jobs", params={"cursor": cursor}, headers=history_account.headers
    )

    assert response.status_code == 422


def test_job_history_normalizes_cursor_timezone(
    client, history_account, test_session_factory
):
    with test_session_factory() as session:
        old_id = str(add_history_job(session, history_account.user_id).id)
        boundary = add_history_job(
            session,
            history_account.user_id,
            created_at=BASE_TIME + timedelta(hours=1),
        )
        cursor = raw_cursor(
            {"created_at": "2026-10-16T11:00:00+02:00", "id": str(boundary.id)}
        )
        session.commit()

    response = client.get(
        "/jobs", params={"cursor": cursor}, headers=history_account.headers
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [old_id]
    assert response.json()["next_cursor"] is None
