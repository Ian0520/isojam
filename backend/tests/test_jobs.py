import app.storage as storage
import app.repositories.jobs as jobs
import app.repositories.uploads as uploads
import app.repositories.job_outputs as job_outputs
from app.security import create_access_token
import app.main as main
from app.db_models import Job, JobOutput
from tests.factories import create_test_user, create_test_upload, create_test_job


from sqlalchemy import select
from unittest.mock import Mock
from uuid import uuid4, UUID
import pytest
from datetime import timedelta

@pytest.fixture
def uploaded_file_id(client, tmp_path, monkeypatch, auth_headers):
    monkeypatch.setattr(storage, "UPLOAD_DIR", tmp_path)
    output_root = tmp_path / "outputs"
    monkeypatch.setattr(storage, "OUTPUT_DIR", output_root)

    upload_response = client.post(
        "/uploads",
        files={
            "audio_file": (
                "test.wav",
                b"fake audio data",
                "audio/wav",
            )
        },
        headers=auth_headers,
    )

    assert upload_response.status_code == 200

    upload_id = upload_response.json()["id"]
    return upload_id

def test_create_job(client, uploaded_file_id, test_session_factory, auth_headers):
    upload_id = uploaded_file_id

    create_response = client.post(
        "/jobs",
        json={
            "upload_id": upload_id
        },
        headers=auth_headers,
    )
    assert create_response.status_code == 200

    data = create_response.json()
    assert data["id"] != ""
    assert data["status"] == "pending"
    assert data["upload_id"] == upload_id
    assert data["outputs"] == {}

    job_id = UUID(data["id"])
    with test_session_factory() as session:
        processed_job = jobs.get_job(session, job_id)
        assert processed_job.status == "completed"
        outputs = job_outputs.get_job_outputs(session, job_id)
        assert len(outputs) == 1
        output = outputs[0]
        assert output.stem == "guitar"
        assert output.path == str(storage.get_job_output_dir(job_id) / "test_guitar.wav")



def test_create_job_not_found(client, auth_headers):
    fake_upload_id = str(uuid4())
    response = client.post(
        "/jobs",
        json={
            "upload_id": fake_upload_id
        },
        headers=auth_headers,
    )
    assert response.status_code == 404
    data = response.json()
    assert data["detail"] == "The upload does not exist"


def test_get_job(client, uploaded_file_id, auth_headers):
    upload_id = uploaded_file_id   
    
    create_response = client.post(
        "/jobs",
        json={
            "upload_id": upload_id
        },
        headers=auth_headers,
    )
    assert create_response.status_code == 200
    created_job = create_response.json()
    job_id = created_job["id"]
    get_response = client.get(
        f"/jobs/{job_id}",
        headers=auth_headers,
        )

    assert get_response.status_code == 200

    retrieved_job = get_response.json()
    assert retrieved_job["id"] == job_id
    assert retrieved_job["status"] == "completed"
    assert retrieved_job["upload_id"] == upload_id

    assert retrieved_job["outputs"]["guitar"] == f"/jobs/{job_id}/outputs/guitar"

def test_get_job_not_found(client, auth_headers):
    fake_job_id = str(uuid4())
    response = client.get(
        f"/jobs/{fake_job_id}",
        headers=auth_headers,
        )

    assert response.status_code == 404

    data = response.json()
    assert data["detail"] == "The requested job does not exist"

def test_download_job_output(
    client,
    uploaded_file_id,
    auth_headers,
):
    upload_id = uploaded_file_id
    create_response = client.post(
        "/jobs",
        json={
            "upload_id": upload_id
        },
        headers=auth_headers,
    )
    assert create_response.status_code == 200
    
    job_id = create_response.json()["id"]
    output_path = storage.get_job_output_dir(job_id) / "test_guitar.wav"
    output_path.write_bytes(b"fake audio")

    get_response = client.get(
        f"/jobs/{job_id}/outputs/guitar",
        headers=auth_headers,
        )
    assert get_response.status_code == 200
    assert get_response.content == b"fake audio"

    
def test_download_output_for_missing_job(client, auth_headers):
    fake_job_id = uuid4()
    response = client.get(
        f"/jobs/{fake_job_id}/outputs/guitar",
        headers=auth_headers,
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "The job does not exist"

def test_download_output_for_incomplete_job(
    client,
    test_session_factory,
    jwt_secret_key,
):
    with test_session_factory() as session:
        user = create_test_user(session)
        user_id = user.id
        upload = create_test_upload(session, user)
        job = create_test_job(session, upload)

        job_id = job.id
        session.commit()

    access_token = create_access_token(
        user_id=user_id,
        secret_key=jwt_secret_key,
        expires_delta=timedelta(minutes=5),
    )

    response = client.get(
        f"/jobs/{job_id}/outputs/guitar",
        headers={"Authorization": f"Bearer {access_token}"},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "The job is not completed"


def test_download_missing_stem(client, uploaded_file_id, auth_headers):
    upload_id = uploaded_file_id
    create_response = client.post(
        "/jobs",
        json={
            "upload_id": upload_id
        },
        headers=auth_headers,
    )
    assert create_response.status_code == 200
    job_id = create_response.json()["id"]

    download_response = client.get(
        f"/jobs/{job_id}/outputs/violin",
        headers=auth_headers,
        )
    assert download_response.status_code == 404
    assert download_response.json()["detail"] == "The requested stem does not exist"

def test_download_output_when_file_is_missing(client, uploaded_file_id, auth_headers):
    upload_id = uploaded_file_id
    create_response = client.post(
        "/jobs",
        json={"upload_id": upload_id},
        headers=auth_headers,
    )
    assert create_response.status_code == 200
    job_id = create_response.json()["id"]

    download_response = client.get(
        f"/jobs/{job_id}/outputs/guitar",
        headers=auth_headers,
        )

    assert download_response.status_code == 404
    assert download_response.json()["detail"] == "The output file is missing"

def test_create_job_requires_authentication(client):
    response = client.post(
        "/jobs",
        json={"upload_id": str(uuid4())},
    )

    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"

def test_create_job_rejects_another_users_upload(
    client,
    test_session_factory,
    jwt_secret_key,
    monkeypatch,
):
    with test_session_factory() as session:
        alice = create_test_user(session, "alice@example.com")
        bob = create_test_user(session, "bob@example.com")
        bob_upload = create_test_upload(session,user=bob)
        alice_id = alice.id
        bob_upload_id = bob_upload.id
        session.commit()

    alice_access_token = create_access_token(
        user_id=alice_id,
        secret_key=jwt_secret_key,
        expires_delta=timedelta(minutes=5)
        )

    process_job_mock = Mock()
    monkeypatch.setattr(main, "process_job", process_job_mock)

    response = client.post(
        "/jobs",
        json={"upload_id": str(bob_upload_id)},
        headers={"Authorization": f"Bearer {alice_access_token}"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "The upload does not exist"
    process_job_mock.assert_not_called()
    with test_session_factory() as session:
        assert session.scalars(select(Job)).first() is None

def test_get_job_requires_authentication(client):
    job_id = uuid4()
    response = client.get(
        f"/jobs/{job_id}",
    )
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"

def test_get_job_rejects_another_users_job(
    client,
    test_session_factory,
    jwt_secret_key,
):
    with test_session_factory() as session:
        alice = create_test_user(session, "alice@example.com")
        bob = create_test_user(session, "bob@example.com")
        bob_upload = create_test_upload(session, bob)
        bob_job = create_test_job(session, bob_upload)
        bob_job_id = bob_job.id
        alice_user_id = alice.id

        session.commit()

    access_token = create_access_token(
        user_id=alice_user_id,
        secret_key=jwt_secret_key,
        expires_delta=timedelta(minutes=5),
    )

    response = client.get(
        f"/jobs/{bob_job_id}",
        headers={"Authorization": f"Bearer {access_token}"}
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "The requested job does not exist"

def test_download_job_output_requires_authentication(client):
    response = client.get(
        f"/jobs/{uuid4()}/outputs/guitar",
    )
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"

def test_download_job_output_rejects_another_users_job(
    client,
    test_session_factory,
    jwt_secret_key,
):
    with test_session_factory() as session:
        alice = create_test_user(session, "alice@example.com")
        bob = create_test_user(session, "bob@example.com")
        bob_upload = create_test_upload(session, bob)
        bob_job = create_test_job(session, bob_upload)
        bob_job_id = bob_job.id
        alice_user_id = alice.id

        session.commit()

    access_token = create_access_token(
        user_id=alice_user_id,
        secret_key=jwt_secret_key,
        expires_delta=timedelta(minutes=5),
    )

    response = client.get(
        f"/jobs/{bob_job_id}/outputs/guitar",
        headers={"Authorization": f"Bearer {access_token}"}
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "The job does not exist"

def test_download_rejects_another_users_completed_output(
    client,
    test_session_factory,
    jwt_secret_key,
    tmp_path,
):
    output_path = tmp_path / "guitar.wav"
    output_path.write_bytes(b"fake audio data")

    with test_session_factory() as session:
        alice = create_test_user(session, "alice@example.com")
        alice_id = alice.id
        bob = create_test_user(session, "bob@example.com")
        bob_upload = create_test_upload(session, bob)
        bob_job = create_test_job(session, bob_upload, "completed")
        bob_job_id = bob_job.id

        job_output = JobOutput(
            job_id=bob_job_id,
            stem="guitar",
            path=str(output_path),
        )
        session.add(job_output)
        session.commit()

    access_token = create_access_token(
        user_id=alice_id,
        secret_key=jwt_secret_key,
        expires_delta=timedelta(minutes=5)
    )

    response = client.get(
        f"/jobs/{bob_job_id}/outputs/guitar",
        headers={"Authorization": f"Bearer {access_token}"}
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "The job does not exist"