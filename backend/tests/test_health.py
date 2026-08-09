from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_health():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status" : "ok"}

def test_upload_file():
    response = client.post("/uploads", 
                           files={
                               "audio_file": (
                                   "test.wav",
                                   b"fake audio data",
                                   "audio/wav",
                               )
                           })

    assert response.status_code == 200
    assert response.json() == {
        "filename": "test.wav",
        "content_type": "audio/wav",
    }