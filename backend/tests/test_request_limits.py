import asyncio

import pytest
import starlette.formparsers as formparsers
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.storage as storage
from app.db_models import Upload, User
from app.main import create_app

UPLOAD_OVERHEAD = 64 * 1024
API_BODY_LIMIT = 16 * 1024
BOUNDARY = b"isojam-test-boundary"


def multipart_body(audio, *, padding_size=0):
    return (
        b"--"
        + BOUNDARY
        + b'\r\nContent-Disposition: form-data; name="audio_file"; filename="test.wav"\r\n'
        b"Content-Type: audio/wav\r\n\r\n" + audio + b"\r\n"
        b"--"
        + BOUNDARY
        + b'\r\nContent-Disposition: form-data; name="padding"\r\n\r\n'
        + b"x" * padding_size
        + b"\r\n--"
        + BOUNDARY
        + b"--\r\n"
    )


def send_raw_request(
    app, chunks, *, headers, path="/uploads", root_path="", disconnect=False
):
    async def run():
        messages = []
        reads = 0
        chunk_iterator = iter(chunks)

        async def receive():
            nonlocal reads
            reads += 1
            try:
                body = next(chunk_iterator)
            except StopIteration:
                if disconnect:
                    return {"type": "http.disconnect"}
                raise AssertionError("Application read past the end of the request")
            return {
                "type": "http.request",
                "body": body,
                "more_body": reads < len(chunks) or disconnect,
            }

        async def send(message):
            messages.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "root_path": root_path,
            "query_string": b"",
            "headers": [
                (name.lower().encode(), value.encode())
                for name, value in headers.items()
            ],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
        }
        await app(scope, receive, send)
        return messages, reads

    messages, reads = asyncio.run(run())
    status = next(
        message["status"]
        for message in messages
        if message["type"] == "http.response.start"
    )
    return status, reads


@pytest.fixture
def limited_client(
    fake_model_session,
    test_session_factory,
    jwt_secret_key,
    wav_bytes,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(storage, "UPLOAD_DIR", tmp_path / "uploads")
    test_app = create_app(
        model_session_factory=lambda: fake_model_session,
        db_session_factory=test_session_factory,
        jwt_secret_key=jwt_secret_key,
        max_upload_bytes=len(wav_bytes),
    )
    with TestClient(test_app) as client:
        yield client


@pytest.mark.parametrize(
    "path, root_path", [("/uploads", ""), ("/uploads/", ""), ("/api/uploads", "/api")]
)
def test_declared_oversized_upload_is_rejected_without_reading_body(
    limited_client, wav_bytes, monkeypatch, path, root_path
):
    def unexpected_tempfile(*args, **kwargs):
        pytest.fail(
            "Oversized declared body must not create a multipart temporary file"
        )

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", unexpected_tempfile)
    status, reads = send_raw_request(
        limited_client.app,
        [b"unread body"],
        path=path,
        root_path=root_path,
        headers={
            "Content-Type": "multipart/form-data; boundary=isojam-test-boundary",
            "Content-Length": str(len(wav_bytes) + UPLOAD_OVERHEAD + 1),
        },
    )
    assert status == 413
    assert reads == 0


@pytest.mark.parametrize("declared_length", [None, "1", "invalid"])
def test_streamed_oversized_upload_stops_reading_and_closes_parser_files(
    limited_client,
    wav_bytes,
    monkeypatch,
    tmp_path,
    test_session_factory,
    auth_headers,
    declared_length,
):
    created_files = []
    original_tempfile = formparsers.SpooledTemporaryFile

    def tracked_tempfile(*args, **kwargs):
        file = original_tempfile(*args, **kwargs, dir=tmp_path)
        created_files.append(file)
        return file

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", tracked_tempfile)
    monkeypatch.setattr(formparsers.MultiPartParser, "spool_max_size", 1)
    prefix = multipart_body(wav_bytes).split(b"\r\n--" + BOUNDARY, 1)[0]
    headers = {
        **auth_headers,
        "Content-Type": "multipart/form-data; boundary=isojam-test-boundary",
    }
    if declared_length is not None:
        headers["Content-Length"] = declared_length
    status, reads = send_raw_request(
        limited_client.app,
        [prefix, b"x" * (len(wav_bytes) + UPLOAD_OVERHEAD), b"must not be read"],
        headers=headers,
    )
    assert status == 413
    assert reads == 2
    assert created_files
    assert all(file.closed for file in created_files)
    assert list((tmp_path / "uploads").glob("*")) == []
    with test_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Upload)) == 0


@pytest.mark.parametrize(
    "limit_offset, expected_status", [(-1, 200), (0, 200), (1, 413)]
)
def test_upload_request_body_boundary(
    limited_client, wav_bytes, auth_headers, limit_offset, expected_status
):
    limit = len(wav_bytes) + UPLOAD_OVERHEAD
    body = multipart_body(
        wav_bytes, padding_size=limit - len(multipart_body(wav_bytes)) + limit_offset
    )
    status, _ = send_raw_request(
        limited_client.app,
        [body[:200], body[200:]],
        headers={
            **auth_headers,
            "Content-Type": "multipart/form-data; boundary=isojam-test-boundary",
        },
    )
    assert status == expected_status


@pytest.mark.parametrize(
    "path, root_path", [("/uploads", ""), ("/uploads/", ""), ("/api/uploads", "/api")]
)
def test_upload_routes_accept_body_larger_than_json_limit(
    limited_client, wav_bytes, auth_headers, path, root_path
):
    body = multipart_body(wav_bytes, padding_size=API_BODY_LIMIT)
    status, _ = send_raw_request(
        limited_client.app,
        [body],
        path=path,
        root_path=root_path,
        headers={
            **auth_headers,
            "Content-Type": "multipart/form-data; boundary=isojam-test-boundary",
            "Content-Length": str(len(body)),
        },
    )
    assert status == (307 if path.endswith("/") else 200)


@pytest.mark.parametrize("path", ["/register", "/login", "/jobs"])
@pytest.mark.parametrize("declared_length", [None, "1", str(API_BODY_LIMIT + 1)])
def test_other_api_requests_have_smaller_body_limit(
    limited_client, test_session_factory, path, declared_length
):
    headers = {"Content-Type": "application/json"}
    if declared_length is not None:
        headers["Content-Length"] = declared_length
    with test_session_factory() as session:
        existing_users = session.scalar(select(func.count()).select_from(User))
    status, _ = send_raw_request(
        limited_client.app, [b" " * API_BODY_LIMIT, b" "], path=path, headers=headers
    )
    assert status == 413
    with test_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(User)) == existing_users


def test_api_request_exactly_at_limit_reaches_endpoint(limited_client):
    body = b'{"email":"invalid","password":"invalid"}'
    body += b" " * (API_BODY_LIMIT - len(body))
    status, _ = send_raw_request(
        limited_client.app,
        [body],
        path="/register",
        headers={"Content-Type": "application/json"},
    )
    assert status == 422


def test_upload_disconnect_closes_parser_files(
    limited_client, wav_bytes, auth_headers, monkeypatch, tmp_path
):
    created_files = []
    original_tempfile = formparsers.SpooledTemporaryFile

    def tracked_tempfile(*args, **kwargs):
        file = original_tempfile(*args, **kwargs, dir=tmp_path)
        created_files.append(file)
        return file

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", tracked_tempfile)
    monkeypatch.setattr(formparsers.MultiPartParser, "spool_max_size", 1)
    prefix = multipart_body(wav_bytes).split(b"\r\n--" + BOUNDARY, 1)[0]
    status, _ = send_raw_request(
        limited_client.app,
        [prefix],
        headers={
            **auth_headers,
            "Content-Type": "multipart/form-data; boundary=isojam-test-boundary",
        },
        disconnect=True,
    )
    assert status == 400
    assert created_files
    assert all(file.closed for file in created_files)
