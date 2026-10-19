import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import soundfile as sf

import app.youtube as youtube
from app.youtube import YouTubeImportError, import_youtube_audio, youtube_video_id

VIDEO_ID = "BaW_jenozKc"


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.youtube.com/watch?v={VIDEO_ID}",
        f"https://youtube.com/watch?v={VIDEO_ID}&list=ignored&index=2#t=10",
        f"https://m.youtube.com/watch?feature=share&v={VIDEO_ID}",
        f"https://music.youtube.com/watch?v={VIDEO_ID}",
        f"https://youtu.be/{VIDEO_ID}?si=ignored",
        f"https://www.youtube.com/shorts/{VIDEO_ID}",
        f"https://www.youtube.com/embed/{VIDEO_ID}/",
    ],
)
def test_extracts_one_video_id_without_preserving_other_url_fields(url):
    assert youtube_video_id(url) == VIDEO_ID


@pytest.mark.parametrize(
    "url",
    [
        "",
        VIDEO_ID,
        "http://youtube.com/watch?v=" + VIDEO_ID,
        "https://www.youtube.com/playlist?list=anything",
        "https://www.youtube.com/@channel",
        "https://www.youtube.com/watch?v=short",
        f"https://youtube.com/watch?v={VIDEO_ID}&v={VIDEO_ID}",
        f"https://youtube.com.evil.test/watch?v={VIDEO_ID}",
        f"https://youtube.com@127.0.0.1/watch?v={VIDEO_ID}",
        f"https://user@youtube.com/watch?v={VIDEO_ID}",
        f"https://youtube.com:443/watch?v={VIDEO_ID}",
        f"https://youtube.com:invalid/watch?v={VIDEO_ID}",
        f"https://youtu.be/{VIDEO_ID}/extra",
        f"https://youtube.com/\nwatch?v={VIDEO_ID}",
        "file:///tmp/audio.wav",
        "https://127.0.0.1/audio",
        "x" * 2049,
    ],
)
def test_rejects_non_video_or_ambiguous_urls(url):
    with pytest.raises(ValueError, match="YouTube video URL"):
        youtube_video_id(url)


@pytest.fixture
def downloader(monkeypatch, encoded_audio):
    def configure(suffix=".m4a", *, frames=4410, payload=None):
        source = encoded_audio(suffix, frames=frames)

        def download(video_id, directory, **limits):
            assert video_id == VIDEO_ID
            (directory / f"source{suffix}").write_bytes(
                payload if payload is not None else source.read_bytes()
            )
            return {"title": "My recording"}

        monkeypatch.setattr(youtube, "_run_download", download)
        return download

    return configure


def run_import(directory, **limits):
    return import_youtube_audio(
        f"https://youtu.be/{VIDEO_ID}",
        directory,
        **(
            {
                "max_download_bytes": 100000,
                "max_duration_seconds": 1,
                "max_converted_bytes": 400000,
            }
            | limits
        ),
    )


@pytest.mark.parametrize("suffix", [".m4a", ".webm"])
def test_import_publishes_only_a_validated_wav(downloader, tmp_path, suffix):
    downloader(suffix)
    directory = tmp_path / "imports"
    result = run_import(directory)
    assert result.video_id == VIDEO_ID and result.title == "My recording"
    assert list(directory.iterdir()) == [result.path]
    info = sf.info(result.path)
    assert info.format == "WAV" and info.subtype == "FLOAT"
    assert info.samplerate == 44100 and info.channels == 2 and info.frames > 0


@pytest.mark.parametrize(
    "failure", ["download", "duration", "expanded", "invalid", "source_size"]
)
def test_failed_import_preserves_existing_files_and_leaves_no_partials(
    downloader,
    tmp_path,
    monkeypatch,
    failure,
):
    downloader(
        ".m4a",
        frames=88200 if failure == "duration" else 4410,
        payload=b"not audio" if failure == "invalid" else None,
    )
    limits = {}
    if failure == "expanded":
        limits["max_converted_bytes"] = 10
    elif failure == "source_size":
        limits["max_download_bytes"] = 1
    elif failure == "download":

        def failed_download(video_id, directory, **limits):
            (directory / "source.m4a.part").write_bytes(b"partial")
            raise YouTubeImportError("unavailable", "Video unavailable")

        monkeypatch.setattr(youtube, "_run_download", failed_download)
    directory = tmp_path / "imports"
    directory.mkdir()
    existing = directory / "existing.wav"
    existing.write_bytes(b"existing recording")
    with pytest.raises(YouTubeImportError):
        run_import(directory, **limits)
    assert list(directory.iterdir()) == [existing]
    assert existing.read_bytes() == b"existing recording"


def test_import_never_overwrites_existing_destination(
    downloader, tmp_path, monkeypatch
):
    downloader()
    monkeypatch.setattr(youtube, "uuid4", lambda: "existing")
    destination = tmp_path / "existing.wav"
    destination.write_bytes(b"keep me")
    with pytest.raises(FileExistsError):
        run_import(tmp_path)
    assert destination.read_bytes() == b"keep me"
    assert list(tmp_path.glob(".youtube-*")) == []


def test_import_rejects_symlinked_download(tmp_path, monkeypatch):
    existing = tmp_path / "existing.m4a"
    existing.write_bytes(b"keep me")

    def symlinked_download(video_id, directory, **limits):
        (directory / "source.m4a").symlink_to(existing)
        return {"title": "recording"}

    monkeypatch.setattr(youtube, "_run_download", symlinked_download)
    with pytest.raises(YouTubeImportError):
        run_import(tmp_path / "imports")
    assert existing.read_bytes() == b"keep me"
    assert list((tmp_path / "imports").iterdir()) == []


def test_timeout_stops_downloader_and_its_child_process(tmp_path, monkeypatch):
    original_popen = subprocess.Popen
    processes = []
    script = """
import subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
Path(sys.argv[1], 'child.pid').write_text(str(child.pid))
time.sleep(60)
"""

    def slow_download(command, **kwargs):
        process = original_popen(
            [sys.executable, "-c", script, str(tmp_path)], **kwargs
        )
        processes.append(process)
        return process

    monkeypatch.setattr(youtube.subprocess, "Popen", slow_download)
    monkeypatch.setattr(youtube, "DOWNLOAD_TIMEOUT_SECONDS", 0.5)
    with pytest.raises(YouTubeImportError, match="time limit"):
        youtube._run_download(
            VIDEO_ID, tmp_path, max_bytes=100000, max_duration_seconds=1
        )
    assert processes[0].poll() is not None
    child_pid = int((tmp_path / "child.pid").read_text())
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        state = Path(f"/proc/{child_pid}/stat")
        if not state.exists() or state.read_text().split()[2] == "Z":
            break
        time.sleep(0.01)
    else:
        pytest.fail("Downloader descendant survived timeout")


@pytest.mark.parametrize(
    "response", [{"error": "tooling"}, {"error": "unavailable"}, {"error": []}, []]
)
def test_downloader_failures_have_stable_errors(tmp_path, monkeypatch, response):
    original_popen = subprocess.Popen
    script = f"print({json.dumps(json.dumps(response))})"

    def failing_download(command, **kwargs):
        return original_popen([sys.executable, "-c", script], **kwargs)

    monkeypatch.setattr(youtube.subprocess, "Popen", failing_download)
    with pytest.raises(YouTubeImportError):
        youtube._run_download(
            VIDEO_ID, tmp_path, max_bytes=100000, max_duration_seconds=1
        )


def test_downloader_and_descendant_stop_when_supervising_importer_is_killed(tmp_path):
    child_source = """
import subprocess, sys, time
from pathlib import Path
from app import youtube_download as child
def slow_download(video_id, directory, **limits):
    descendant = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    (directory / 'descendant.pid').write_text(str(descendant.pid))
    (directory / 'downloader.pid').write_text(str(__import__('os').getpid()))
    time.sleep(60)
child.download = slow_download
raise SystemExit(child.main())
"""
    parent_source = f"""
import subprocess, sys
from pathlib import Path
from app import youtube
from app.import_worker import worker_lock
original = subprocess.Popen
def launch(command, **kwargs):
    return original([sys.executable, '-c', {child_source!r}, *command[3:]], **kwargs)
youtube.subprocess.Popen = launch
directory = Path(sys.argv[1])
with worker_lock(directory / 'locks') as lock_fd:
    youtube._run_download('{VIDEO_ID}', directory, max_bytes=100000,
                          max_duration_seconds=1, lock_fd=lock_fd)
"""
    parent = subprocess.Popen([sys.executable, "-c", parent_source, str(tmp_path)])
    pids = []
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / "downloader.pid").exists():
            assert parent.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        pids = [
            int((tmp_path / name).read_text())
            for name in ("downloader.pid", "descendant.pid")
        ]
        parent.kill()
        parent.wait(timeout=5)
        deadline = time.monotonic() + 3
        for pid in pids:
            while time.monotonic() < deadline:
                path = Path(f"/proc/{pid}/stat")
                if not path.exists() or path.read_text().split()[2] == "Z":
                    break
                time.sleep(0.01)
            else:
                pytest.fail("Downloader process survived supervisor death")
        from app.import_worker import worker_lock

        with worker_lock(tmp_path / "locks"):
            pass
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        # Only the dedicated test downloader group can own this PID here.
        if pids:
            path = Path(f"/proc/{pids[0]}/stat")
            if path.exists() and path.read_text().split()[2] != "Z":
                os.killpg(pids[0], signal.SIGKILL)
