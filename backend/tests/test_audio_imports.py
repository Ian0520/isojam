import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update

import app.import_worker as worker
from app import config
from app.database import commit_session
from app.db_models import AudioImport, Upload
from app.main import create_app
from app.repositories import audio_imports
from app.security import create_access_token
from app.youtube import ImportedAudio, YouTubeImportError
from tests.factories import create_test_user, make_wav_bytes

URL = "https://youtu.be/BaW_jenozKc"
VIDEO = "BaW_jenozKc"
LIMITS = dict(
    max_download_bytes=100000, max_duration_seconds=2, max_converted_bytes=200000
)


@pytest.fixture
def import_client(client):
    client.app.state.youtube_imports_enabled = True
    return client


def submit(client, headers, *, key="import-1", url=URL):
    return client.post(
        "/imports", json={"url": url}, headers=headers | {"Idempotency-Key": key}
    )


def enqueue(sessions, *, email="importer@example.com", key="one"):
    with sessions() as session:
        user = create_test_user(session, email)
        item = audio_imports.admit(
            session, user.id, key, VIDEO, per_user_limit=1, global_limit=8
        )
        item_id = item.id
        commit_session(session)
    return item_id


def fake_importer(url, directory, **limits):
    path = directory / "finished.wav"
    path.write_bytes(make_wav_bytes())
    return ImportedAudio(VIDEO, "My /recording\n", path)


def test_import_accepts_canonical_video_and_replays_even_when_disabled(
    import_client, auth_headers, test_session_factory
):
    response = submit(import_client, auth_headers)
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending" and body["upload_id"] is None
    import_client.app.state.youtube_imports_enabled = False
    replay = submit(
        import_client,
        auth_headers,
        url=f"https://youtube.com/watch?v={VIDEO}&list=ignored",
    )
    assert replay.status_code == 202 and replay.json() == body
    assert submit(import_client, auth_headers, key="new").status_code == 503
    with test_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AudioImport)) == 1
        assert session.scalar(select(func.count()).select_from(Upload)) == 0


def test_import_key_conflict_and_unfinished_allowance(import_client, auth_headers):
    assert submit(import_client, auth_headers).status_code == 202
    assert (
        submit(
            import_client, auth_headers, url="https://youtu.be/YE7VzlLtp-4"
        ).status_code
        == 409
    )
    assert submit(import_client, auth_headers, key="different").status_code == 429


@pytest.mark.parametrize(
    "url",
    [
        "http://youtube.com/watch?v=" + VIDEO,
        "https://127.0.0.1/audio",
        "file:///etc/passwd",
        "https://youtube.com/playlist?list=123",
    ],
)
def test_import_rejects_urls_before_queueing(import_client, auth_headers, url):
    assert submit(import_client, auth_headers, url=url).status_code == 422


def test_import_requires_auth_and_a_valid_retry_key(import_client, auth_headers):
    assert submit(import_client, {}).status_code == 401
    assert (
        import_client.post(
            "/imports", json={"url": URL}, headers=auth_headers
        ).status_code
        == 422
    )
    for key in ("", "contains spaces", "x" * 129):
        assert submit(import_client, auth_headers, key=key).status_code == 422


def test_import_ownership_and_bounded_recent_list(
    import_client, auth_headers, jwt_secret_key, test_session_factory
):
    own = submit(import_client, auth_headers).json()
    with test_session_factory() as session:
        other = create_test_user(session, "other@example.com")
        other_id = other.id
        session.commit()
    token = create_access_token(
        user_id=other_id, secret_key=jwt_secret_key, expires_delta=timedelta(minutes=5)
    )
    other_headers = {"Authorization": "Bearer " + token}
    assert (
        import_client.get("/imports/" + own["id"], headers=other_headers).status_code
        == 404
    )
    assert (
        import_client.get("/imports/" + str(uuid4()), headers=auth_headers).status_code
        == 404
    )
    assert import_client.get("/imports", headers=other_headers).json() == []
    assert import_client.get("/imports?limit=1", headers=auth_headers).json() == [own]
    assert (
        import_client.get("/imports?limit=51", headers=auth_headers).status_code == 422
    )


@pytest.mark.parametrize("same_key", [False, True])
def test_concurrent_admission_honors_limits_and_idempotency(
    invitation_sessions, same_key
):
    sessions = invitation_sessions
    with sessions() as session:
        user = create_test_user(session)
        user_id = user.id
        session.commit()
    barrier = Barrier(2)

    def attempt(index):
        with sessions() as session:
            barrier.wait(timeout=5)
            item = audio_imports.admit(
                session,
                user_id,
                "same" if same_key else str(index),
                VIDEO,
                per_user_limit=1,
                global_limit=8,
            )
            item_id = item.id if item is not None else None
            commit_session(session)
            return item_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        result = list(executor.map(attempt, range(2)))
    if same_key:
        assert result[0] is not None and result[0] == result[1]
    else:
        assert sum(item is not None for item in result) == 1


def test_global_allowance_is_shared_between_users(invitation_sessions):
    sessions = invitation_sessions
    with sessions() as session:
        users = [create_test_user(session, f"user{i}@example.com").id for i in range(2)]
        session.commit()
    barrier = Barrier(2)

    def attempt(user_id):
        with sessions() as session:
            barrier.wait(timeout=5)
            item = audio_imports.admit(
                session, user_id, "one", VIDEO, per_user_limit=2, global_limit=1
            )
            accepted = item is not None
            commit_session(session)
            return accepted

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sum(executor.map(attempt, users)) == 1


def test_worker_publishes_owned_upload_without_holding_write_transaction(
    invitation_sessions, tmp_path
):
    sessions = invitation_sessions
    import_id = enqueue(sessions)
    audio = tmp_path / "audio"

    def import_with_database_write(*args, **kwargs):
        # A second writer must be able to proceed while network/decoding runs.
        with sessions() as session:
            session.execute(
                update(AudioImport)
                .where(AudioImport.id == import_id)
                .values(video_id=VIDEO)
            )
            session.rollback()
        return fake_importer(*args, **kwargs)

    assert worker.process_next(
        sessions, audio, importer=import_with_database_write, **LIMITS
    )
    with sessions() as session:
        item = session.get(AudioImport, import_id)
        assert item.status == "ready" and item.error_code is None
        upload = session.get(Upload, item.upload_id)
        assert upload.user_id == item.user_id
        assert upload.original_filename == "My recording.wav"
        assert (
            audio / "uploads" / upload.stored_filename
        ).read_bytes() == make_wav_bytes()
    assert not (audio / "imports" / str(import_id)).exists()
    assert not worker.process_next(sessions, audio, importer=fake_importer, **LIMITS)


def test_import_failure_releases_allowance_and_cleans_private_files(
    test_session_factory, tmp_path
):
    sessions = test_session_factory
    import_id = enqueue(sessions)

    def fail(url, directory, **limits):
        (directory / "partial.webm").write_bytes(b"partial")
        raise YouTubeImportError("too_long", "private downloader diagnostics")

    assert worker.process_next(sessions, tmp_path / "audio", importer=fail, **LIMITS)
    with sessions() as session:
        item = session.get(AudioImport, import_id)
        assert item.status == "failed" and item.error_code == "too_long"
        assert item.upload_id is None
        assert session.scalar(select(func.count()).select_from(Upload)) == 0
        assert (
            audio_imports.admit(
                session, item.user_id, "retry", VIDEO, per_user_limit=1, global_limit=1
            )
            is not None
        )
    assert not (tmp_path / "audio" / "imports" / str(import_id)).exists()


def test_recovery_fails_interrupted_work_preserves_pending_and_removes_orphans(
    invitation_sessions, tmp_path, monkeypatch
):
    sessions = invitation_sessions
    import_id = enqueue(sessions)
    pending_id = enqueue(sessions, email="waiting@example.com")
    audio = tmp_path / "audio"

    def broken_upload(*args, **kwargs):
        raise RuntimeError("publication failed")

    monkeypatch.setattr(worker.uploads, "create_upload", broken_upload)
    with pytest.raises(RuntimeError, match="publication failed"):
        worker.process_next(sessions, audio, importer=fake_importer, **LIMITS)
    # Whichever UUID wins the timestamp tie is the interrupted request.
    with sessions() as session:
        interrupted = session.scalar(
            select(AudioImport).where(AudioImport.status == "processing")
        )
        interrupted_id = interrupted.id
        untouched_id = ({import_id, pending_id} - {interrupted_id}).pop()
        assert session.scalar(select(func.count()).select_from(Upload)) == 0
    assert worker._destination(audio, interrupted_id).is_file()
    with worker.worker_lock(audio / "imports"):
        worker.recover_interrupted(sessions, audio)
        worker.recover_interrupted(sessions, audio)
    with sessions() as session:
        interrupted = session.get(AudioImport, interrupted_id)
        assert (
            interrupted.status == "failed" and interrupted.error_code == "interrupted"
        )
        assert session.get(AudioImport, untouched_id).status == "pending"
    assert not worker._destination(audio, interrupted_id).exists()


def test_recovery_keeps_audio_when_commit_succeeds_then_connection_reports_failure(
    test_session_factory, tmp_path, monkeypatch
):
    sessions = test_session_factory
    import_id = enqueue(sessions)
    calls = 0

    def commit_then_error(session):
        nonlocal calls
        calls += 1
        commit_session(session)
        if calls == 2:
            raise RuntimeError("connection lost after commit")

    monkeypatch.setattr(worker, "commit_session", commit_then_error)
    audio = tmp_path / "audio"
    with pytest.raises(RuntimeError, match="connection lost"):
        worker.process_next(sessions, audio, importer=fake_importer, **LIMITS)
    worker.recover_interrupted(sessions, audio)
    with sessions() as session:
        item = session.get(AudioImport, import_id)
        assert (
            item.status == "ready" and session.get(Upload, item.upload_id) is not None
        )
    assert worker._destination(audio, import_id).read_bytes() == make_wav_bytes()


def test_second_worker_cannot_recover_while_first_publisher_is_alive(tmp_path):
    root = tmp_path / "imports"
    with worker.worker_lock(root):
        with pytest.raises(RuntimeError, match="already running"):
            with worker.worker_lock(root):
                pytest.fail("Two workers acquired the same lock")
    with worker.worker_lock(root):
        assert (root / "worker.lock").exists()


def test_ready_import_can_be_submitted_to_existing_job_api(
    import_client, auth_headers, test_session_factory, tmp_path
):
    item = submit(import_client, auth_headers).json()
    worker.process_next(
        test_session_factory, tmp_path / "audio", importer=fake_importer, **LIMITS
    )
    ready = import_client.get("/imports/" + item["id"], headers=auth_headers).json()
    import_client.app.state.processing_mode = "queued"
    response = import_client.post(
        "/jobs",
        json={"upload_id": ready["upload_id"]},
        headers=auth_headers | {"Idempotency-Key": "job"},
    )
    assert response.status_code == 200 and response.json()["status"] == "pending"
    assert response.json()["upload_id"] == ready["upload_id"]
    assert UUID(ready["upload_id"])


@pytest.mark.parametrize("value, expected", [("0", False), ("1", True)])
def test_import_enable_setting_is_explicit(monkeypatch, value, expected):
    monkeypatch.setenv("ISOJAM_YOUTUBE_IMPORTS_ENABLED", value)
    assert config.get_youtube_imports_enabled() is expected


@pytest.mark.parametrize("value", ["", "true", "false", "2"])
def test_import_enable_setting_rejects_ambiguous_values(monkeypatch, value):
    monkeypatch.setenv("ISOJAM_YOUTUBE_IMPORTS_ENABLED", value)
    with pytest.raises(RuntimeError, match="must be '0' or '1'"):
        config.get_youtube_imports_enabled()


@pytest.mark.parametrize(
    "name", ["max_unfinished_imports", "max_unfinished_imports_per_user"]
)
@pytest.mark.parametrize("value", [0, -1, True, "1"])
def test_import_limits_reject_invalid_injections(name, value):
    with pytest.raises(ValueError, match="positive integer"):
        create_app(**{name: value})


def test_worker_sigterm_finishes_current_import_without_claiming_next(
    test_session_factory, tmp_path
):
    sessions = test_session_factory
    first_id = enqueue(sessions)
    source = """
import time
from pathlib import Path
import app.import_worker as w
from app.youtube import ImportedAudio
from tests.factories import make_wav_bytes
root = Path(__import__('sys').argv[1])
w.READY_FILE = root / 'ready.json'
def importer(url, directory, **limits):
    (root / 'started').touch()
    while not (root / 'release').exists():
        time.sleep(0.02)
    path = directory / 'finished.wav'
    path.write_bytes(make_wav_bytes())
    return ImportedAudio('BaW_jenozKc', 'Finished', path)
original = w.process_next
w.process_next = lambda *args, **kwargs: original(*args, importer=importer, **kwargs)
w.serve()
"""
    env = os.environ | {
        "ISOJAM_DATABASE_PATH": str(sessions.kw["bind"].url.database),
        "ISOJAM_AUDIO_STORAGE_DIR": str(tmp_path / "audio"),
    }
    process = subprocess.Popen(
        [sys.executable, "-c", source, str(tmp_path)], env=env, start_new_session=True
    )
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / "started").exists():
            assert process.poll() is None, "Worker exited before claiming its import"
            assert time.monotonic() < deadline, "Worker did not start"
            time.sleep(0.02)
        second_id = enqueue(sessions, email="second@example.com")
        process.send_signal(signal.SIGTERM)
        time.sleep(0.1)
        assert process.poll() is None, "SIGTERM interrupted active import"
        (tmp_path / "release").touch()
        assert process.wait(timeout=10) == 0
        with sessions() as session:
            assert session.get(AudioImport, first_id).status == "ready"
            assert session.get(AudioImport, second_id).status == "pending"
        assert not (tmp_path / "ready.json").exists()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
