from fastapi.testclient import TestClient
from app.main import app
from uuid import uuid4

client = TestClient(app)

def test_create_job():
    response = client.post("/jobs")
    assert response.status_code == 200

    data = response.json()
    assert data["id"] != ""
    assert data["status"] == "pending"

def test_get_job():
    create_response = client.post("/jobs")
    created_job = create_response.json()
    job_id = created_job["id"]
    get_response = client.get(f"/jobs/{job_id}")

    assert get_response.status_code == 200

    retrieved_job = get_response.json()
    assert retrieved_job["id"] == job_id
    assert retrieved_job["status"] == "pending"

def test_get_job_not_found():
    fake_job_id = str(uuid4())
    response = client.get(f"/jobs/{fake_job_id}")

    assert response.status_code == 404

    data = response.json()
    assert data["detail"] == "The requested job does not exist"