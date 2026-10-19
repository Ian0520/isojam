"""Private downloader child: run with a file limit and no database access."""

import json
import math
import os
import re
import resource
import shutil
import signal
import sys
import threading
import time
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

from app.youtube_network import install_network_guard


class DownloadRejected(Exception):
    def __init__(self, code):
        self.code = code


def watch_parent() -> None:
    """Stop this private process group if its supervising importer is killed.

    The normal parent enforces the wall-clock timeout. This independent watcher
    also stops downloader/Deno children when that parent cannot run its finally
    block, so they cannot keep downloading or recreate cleaned work directories.
    """
    value = os.environ.get("ISOJAM_DOWNLOAD_PARENT_PID")
    if value is None:
        return
    parent_pid = int(value)
    group = os.getpid()
    if parent_pid <= 0 or os.getpgrp() != group:
        raise RuntimeError("Downloader requires its own supervised process group")

    def monitor():
        while True:
            if os.getppid() != parent_pid:
                os.killpg(group, signal.SIGKILL)
            time.sleep(0.1)

    threading.Thread(target=monitor, daemon=True).start()


def check_video(info, *, video_id, max_duration_seconds, incomplete=False):
    if incomplete:
        return None
    duration = info.get("duration")
    if (
        info.get("id") != video_id
        or info.get("_type", "video") != "video"
        or info.get("is_live")
        or info.get("live_status") in {"is_live", "is_upcoming", "post_live"}
        or type(duration) not in {int, float}
        or not math.isfinite(duration)
        or duration <= 0
    ):
        raise DownloadRejected("unsupported")
    if duration > max_duration_seconds:
        raise DownloadRejected("too_long")


def select_audio(context, *, max_bytes):
    oversized = False
    for extension in ("m4a", "webm"):
        for candidate in reversed(context.get("formats", [])):
            if (
                candidate.get("ext") != extension
                or candidate.get("vcodec") != "none"
                or candidate.get("acodec") in {None, "none"}
                or candidate.get("protocol") != "https"
            ):
                continue
            codec = candidate["acodec"]
            if not isinstance(codec, str):
                continue
            if extension == "webm" and codec != "opus":
                continue
            if (
                extension == "m4a"
                and codec not in {"aac", "alac"}
                and not re.fullmatch(r"mp4a\.40\.\d+", codec)
            ):
                continue
            size = candidate.get("filesize")
            if type(size) is int and size > max_bytes:
                oversized = True
                continue
            try:
                url = urlsplit(candidate.get("url", ""))
                port = url.port
            except ValueError:
                continue
            if (
                url.scheme == "https"
                and url.hostname
                and url.hostname.endswith(".googlevideo.com")
                and url.username is None
                and url.password is None
                and port is None
            ):
                yield candidate
                return
    raise DownloadRejected("too_large" if oversized else "unsupported")


def download(video_id, directory, *, max_bytes, max_duration_seconds):
    try:
        import yt_dlp
        from yt_dlp.globals import plugin_dirs
        from yt_dlp.utils import DownloadError
    except ImportError:
        raise DownloadRejected("tooling") from None
    if shutil.which("deno") is None:
        raise DownloadRejected("tooling")
    # This is a fresh, dedicated child. The Python embedding API doesn't load
    # CLI config files; clearing plugin paths also prevents ambient extensions.
    plugin_dirs.value = []

    class QuietLogger:
        def debug(self, message):
            pass

        info = debug
        warning = debug
        error = debug

    def match_filter(info, *, incomplete):
        return check_video(
            info,
            video_id=video_id,
            max_duration_seconds=max_duration_seconds,
            incomplete=incomplete,
        )

    def progress(info):
        if info.get("downloaded_bytes", 0) > max_bytes:
            raise DownloadRejected("too_large")

    options = {
        "allowed_extractors": ["youtube"],
        "noplaylist": True,
        "format": partial(select_audio, max_bytes=max_bytes),
        "match_filter": match_filter,
        "outtmpl": str(directory / "source.%(ext)s"),
        "overwrites": False,
        "continuedl": False,
        "cachedir": False,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": QuietLogger(),
        "socket_timeout": 10,
        "retries": 0,
        "extractor_retries": 0,
        "fragment_retries": 0,
        "file_access_retries": 0,
        "buffersize": 65536,
        "noresizebuffer": True,
        "max_filesize": max_bytes,
        "progress_hooks": [progress],
        "proxy": "",
        "usenetrc": False,
        "js_runtimes": {"deno": {}},
        "remote_components": set(),
    }
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(
                f"https://www.youtube.com/watch?v={video_id}",
                download=True,
            )
    except DownloadError:
        if any(
            p.is_file() and p.stat().st_size >= max_bytes for p in directory.iterdir()
        ):
            raise DownloadRejected("too_large") from None
        raise DownloadRejected("unavailable") from None
    if not info:
        raise DownloadRejected("unavailable")
    return {"title": str(info.get("title") or video_id)[:200]}


def main():
    video_id, directory, max_bytes, max_duration = sys.argv[1:]
    try:
        watch_parent()
        max_bytes = int(max_bytes)
        # Enforced by the kernel even if size metadata/progress reporting is wrong.
        resource.setrlimit(resource.RLIMIT_FSIZE, (max_bytes, max_bytes))
        install_network_guard()
        result = download(
            video_id,
            Path(directory),
            max_bytes=max_bytes,
            max_duration_seconds=int(max_duration),
        )
    except DownloadRejected as error:
        result = {"error": error.code}
    except Exception:
        result = {"error": "download_failed"}
    print(json.dumps(result))
    return 1 if "error" in result else 0


if __name__ == "__main__":
    raise SystemExit(main())
