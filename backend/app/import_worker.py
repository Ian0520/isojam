"""Single-host CPU import worker; no model or JWT secret is needed."""

import argparse
import fcntl
import json
import logging
import os
import shutil
import signal
import threading
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from sqlalchemy import select, update

from app.config import (
    get_audio_storage_dir,
    get_max_audio_duration_seconds,
    get_max_converted_audio_bytes,
    get_max_upload_bytes,
)
from app.database import SessionLocal, commit_session
from app.db_models import AudioImport, Upload
from app.repositories import audio_imports, uploads
from app.youtube import YouTubeImportError, import_youtube_audio

logger = logging.getLogger(__name__)
READY_FILE = Path("/tmp/isojam-import-worker-ready.json")


@contextmanager
def worker_lock(root: Path):
    """Keep the inode; the downloader inherits this FD until its writer exits.

    Unlinking the file could let two processes own locks on different inodes.
    """
    root.mkdir(parents=True, exist_ok=True)
    with (root / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("An audio import worker is already running") from None
        yield lock.fileno()


def _destination(audio_dir: Path, import_id: UUID) -> Path:
    return audio_dir / "uploads" / f"youtube-{import_id}.wav"


def recover_interrupted(sessions, audio_dir: Path) -> None:
    """Call only while holding worker_lock, after the old publisher/downloader exit.

    FFmpeg children only emit decoded bytes to the publisher's pipe; they cannot
    write upload metadata or the final path. Private work directories are safe
    to unlink after the publisher and inherited downloader lock are released.
    Interrupted requests fail explicitly; pending requests remain queued.
    """
    with sessions() as session:
        session.execute(
            update(AudioImport)
            .where(AudioImport.status == "processing")
            .values(status="failed", error_code="interrupted")
        )
        commit_session(session)
        settled = session.execute(
            select(AudioImport.id, AudioImport.status).where(
                AudioImport.status.in_(("ready", "failed"))
            )
        ).all()
    for import_id, status in settled:
        if status == "failed":
            destination = _destination(audio_dir, import_id)
            with sessions() as session:
                referenced = session.scalar(
                    select(Upload.id).where(Upload.stored_filename == destination.name)
                )
            if referenced is not None:
                raise RuntimeError("A failed import unexpectedly references an upload")
            destination.unlink(missing_ok=True)
        directory = audio_dir / "imports" / str(import_id)
        if directory.exists():
            shutil.rmtree(directory)


def _filename(title: str, video_id: str) -> str:
    cleaned = "".join(c for c in title if c.isprintable() and c not in "/\\").strip()
    return (cleaned[:180] or video_id) + ".wav"


def process_next(
    sessions,
    audio_dir: Path,
    *,
    max_download_bytes: int,
    max_duration_seconds: int,
    max_converted_bytes: int,
    importer=import_youtube_audio,
    lock_fd: int | None = None,
) -> bool:
    """The caller holds worker_lock for the entire claim/download/publication."""
    with sessions() as session:
        item = audio_imports.claim_next(session)
        if item is None:
            return False
        import_id, user_id, video_id = item.id, item.user_id, item.video_id
        commit_session(session)

    directory = audio_dir / "imports" / str(import_id)
    directory.mkdir(parents=True)
    try:
        download_options = {} if lock_fd is None else {"lock_fd": lock_fd}
        result = importer(
            f"https://www.youtube.com/watch?v={video_id}",
            directory,
            max_download_bytes=max_download_bytes,
            max_duration_seconds=max_duration_seconds,
            max_converted_bytes=max_converted_bytes,
            **download_options,
        )
    except YouTubeImportError as error:
        with sessions() as session:
            item = session.get(AudioImport, import_id)
            item.status, item.error_code = "failed", error.code
            commit_session(session)
        shutil.rmtree(directory)
        logger.info("Audio import %s failed: %s", import_id, error.code)
        return True

    destination = _destination(audio_dir, import_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive, atomic publication on the shared audio filesystem. An import's
    # deterministic name also lets recovery remove an uncommitted orphan.
    os.link(result.path, destination)
    filename = _filename(result.title, video_id)
    with sessions() as session:
        item = session.get(AudioImport, import_id)
        upload = uploads.create_upload(session, filename, destination.name, user_id)
        item.status, item.upload_id, item.filename = "ready", upload.id, filename
        commit_session(session)
    # On an unexpected failure above, leave artifacts and the processing row.
    # Restart recovery first checks committed state, so it cannot delete a WAV
    # whose commit succeeded even if the connection then reported an error.
    shutil.rmtree(directory)
    logger.info("Audio import %s ready", import_id)
    return True


def is_ready() -> bool:
    try:
        pid = json.loads(READY_FILE.read_text())["pid"]
        if type(pid) is not int or pid <= 0:
            return False
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        return command[1:4] == [b"-m", b"app.import_worker", b"serve"]
    except (OSError, ValueError, KeyError, TypeError):
        return False


def serve() -> None:
    audio_dir = get_audio_storage_dir()
    limits = {
        "max_download_bytes": get_max_upload_bytes(),
        "max_duration_seconds": get_max_audio_duration_seconds(),
        "max_converted_bytes": get_max_converted_audio_bytes(),
    }
    stopped = threading.Event()
    # A stop drains the current bounded import, then claims no further work.
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    with worker_lock(audio_dir / "imports") as lock_fd:
        READY_FILE.unlink(missing_ok=True)
        recover_interrupted(SessionLocal, audio_dir)
        READY_FILE.write_text(json.dumps({"pid": os.getpid()}))
        try:
            while not stopped.is_set():
                if not process_next(SessionLocal, audio_dir, lock_fd=lock_fd, **limits):
                    stopped.wait(1)
        finally:
            READY_FILE.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("serve", "health"))
    args = parser.parse_args()
    if args.command == "health":
        return 0 if is_ready() else 1
    logging.basicConfig(level=logging.INFO)
    try:
        serve()
    except Exception:
        logger.exception("Audio import worker stopped; restart after investigating")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
