"""Bounded YouTube audio import, independent of HTTP and separation jobs."""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from app.audio import AudioTooLongError, InvalidAudioError, validate_wav
from app.audio_conversion import (
    ConversionUnavailableError,
    ConvertedAudioTooLargeError,
    convert_to_wav,
)
from app.config import (
    get_max_audio_duration_seconds,
    get_max_converted_audio_bytes,
    get_max_upload_bytes,
)

DOWNLOAD_TIMEOUT_SECONDS = 120
_VIDEO_ID = r"[A-Za-z0-9_-]{11}"


class YouTubeImportError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ImportedAudio:
    video_id: str
    title: str
    path: Path


def youtube_video_id(url: str) -> str:
    """Accept video links, then discard the original URL before any network access."""
    invalid = ValueError("Enter a valid HTTPS YouTube video URL")
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise invalid
    if any(character.isspace() or ord(character) < 32 for character in url):
        raise invalid
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
        ):
            raise invalid
        if parsed.hostname == "youtu.be":
            match = re.fullmatch(rf"/({_VIDEO_ID})/?", parsed.path)
            if match:
                return match[1]
        elif parsed.hostname in {
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "music.youtube.com",
        }:
            if parsed.path == "/watch":
                values = parse_qs(
                    parsed.query, keep_blank_values=True, max_num_fields=30
                ).get("v", [])
                if len(values) == 1 and re.fullmatch(_VIDEO_ID, values[0]):
                    return values[0]
            match = re.fullmatch(rf"/(?:shorts|embed)/({_VIDEO_ID})/?", parsed.path)
            if match:
                return match[1]
    except ValueError:
        raise invalid from None
    raise invalid


def _run_download(
    video_id: str,
    directory: Path,
    *,
    max_bytes: int,
    max_duration_seconds: int,
    lock_fd: int | None = None,
) -> dict:
    command = [
        sys.executable,
        "-m",
        "app.youtube_download",
        video_id,
        str(directory),
        str(max_bytes),
        str(max_duration_seconds),
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        pass_fds=() if lock_fd is None else (lock_fd,),
        env=os.environ
        | {
            "DENO_DIR": str(directory / "deno-cache"),
            "ISOJAM_DOWNLOAD_PARENT_PID": str(os.getpid()),
        },
    )
    try:
        try:
            output, _ = process.communicate(timeout=DOWNLOAD_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            raise YouTubeImportError(
                "timeout", "YouTube download exceeded the time limit"
            ) from error
        try:
            result = json.loads(output)
            if not isinstance(result, dict):
                raise ValueError("Invalid downloader response")
        except (ValueError, TypeError) as error:
            raise YouTubeImportError(
                "download_failed", "YouTube audio could not be downloaded"
            ) from error
        if process.returncode or "error" in result:
            code = result.get("error", "download_failed")
            if not isinstance(code, str):
                code = "download_failed"
            messages = {
                "unavailable": "This video is unavailable or YouTube refused the download",
                "tooling": "YouTube import requires the downloader and Deno runtime",
                "too_large": "Downloaded audio exceeds the maximum allowed size",
                "too_long": "Audio exceeds the maximum allowed duration",
                "unsupported": "Choose a non-live video with a supported audio stream",
            }
            raise YouTubeImportError(
                code, messages.get(code, "YouTube audio could not be downloaded")
            )
        return result
    finally:
        # Signal only an unreaped child: after communicate() finishes, its PID
        # could be reused. On timeout this also stops its Deno descendants.
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()
        process.stdout.close()


def import_youtube_audio(
    url: str,
    output_dir: Path,
    *,
    max_download_bytes: int,
    max_duration_seconds: int,
    max_converted_bytes: int,
    lock_fd: int | None = None,
) -> ImportedAudio:
    video_id = youtube_video_id(url)
    if sys.platform != "linux":
        raise YouTubeImportError(
            "tooling", "Run YouTube imports in Linux or the importer container"
        )
    for value in (max_download_bytes, max_duration_seconds, max_converted_bytes):
        if type(value) is not int or value <= 0:
            raise ValueError("Import limits must be positive integers")
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".youtube-", dir=output_dir) as temporary:
        directory = Path(temporary)
        download_options = {} if lock_fd is None else {"lock_fd": lock_fd}
        result = _run_download(
            video_id,
            directory,
            max_bytes=max_download_bytes,
            max_duration_seconds=max_duration_seconds,
            **download_options,
        )
        sources = [
            p for p in directory.iterdir() if p.name in {"source.m4a", "source.webm"}
        ]
        if len(sources) != 1 or sources[0].is_symlink() or not sources[0].is_file():
            raise YouTubeImportError(
                "download_failed", "YouTube audio could not be downloaded"
            )
        source = sources[0]
        if source.stat().st_size > max_download_bytes:
            raise YouTubeImportError(
                "too_large", "Downloaded audio exceeds the maximum allowed size"
            )
        try:
            wav = convert_to_wav(
                source,
                max_duration_seconds=max_duration_seconds,
                max_bytes=max_converted_bytes,
            )
            validate_wav(wav, max_duration_seconds=max_duration_seconds)
        except AudioTooLongError as error:
            raise YouTubeImportError("too_long", str(error)) from error
        except ConvertedAudioTooLargeError as error:
            raise YouTubeImportError("too_large", str(error)) from error
        except InvalidAudioError as error:
            raise YouTubeImportError("invalid_audio", str(error)) from error
        except ConversionUnavailableError as error:
            raise YouTubeImportError("tooling", str(error)) from error
        destination = output_dir / f"{uuid4()}.wav"
        # The temporary directory is on this same filesystem. Linking publishes
        # the finished file without copying it or overwriting an existing file.
        os.link(wav, destination)
        return ImportedAudio(
            video_id, str(result.get("title") or video_id)[:200], destination
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = import_youtube_audio(
            args.url,
            args.output_dir,
            max_download_bytes=get_max_upload_bytes(),
            max_duration_seconds=get_max_audio_duration_seconds(),
            max_converted_bytes=get_max_converted_audio_bytes(),
        )
    except (YouTubeImportError, ValueError) as error:
        print(
            json.dumps(
                {"error": getattr(error, "code", "invalid_input"), "detail": str(error)}
            ),
            file=sys.stderr,
        )
        return 1
    except OSError:
        print(
            json.dumps(
                {"error": "storage", "detail": "Imported audio could not be stored"}
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "video_id": result.video_id,
                "title": result.title,
                "path": str(result.path),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
