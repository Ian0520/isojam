"""Convert a small allowlist of local audio formats into bounded WAV files."""

import json
import subprocess
from pathlib import Path
from threading import BoundedSemaphore, Event, Timer

import numpy as np
import soundfile as sf

from app.audio import AudioTooLongError, InvalidAudioError

TARGET_SAMPLE_RATE = 44100
CONVERSION_TIMEOUT_SECONDS = 30
PROBE_TIMEOUT_SECONDS = 5
# This protects one API process, rather than imposing a cluster-wide quota.
_conversion_slots = BoundedSemaphore(2)

# suffix: (accepted MIME types, FFmpeg demuxer, accepted codecs)
_FORMATS = {
    ".mp3": (("audio/mpeg", "audio/mp3"), "mp3", {"mp3"}),
    ".m4a": (("audio/mp4", "audio/x-m4a", "audio/m4a"), "mov", {"aac", "alac"}),
    ".aac": (("audio/aac", "audio/x-aac"), "aac", {"aac"}),
    ".flac": (("audio/flac", "audio/x-flac"), "flac", {"flac"}),
}


class ConversionUnavailableError(RuntimeError):
    pass


class ConversionBusyError(RuntimeError):
    pass


class ConvertedAudioTooLargeError(ValueError):
    pass


def get_audio_upload_types() -> dict[str, str]:
    """Canonical multipart types for browser uploads; decoding still verifies content."""
    return {
        ".wav": "audio/wav",
        **{suffix: info[0][0] for suffix, info in _FORMATS.items()},
    }


def supports_audio_upload(filename: str | None, content_type: str | None) -> bool:
    suffix = Path(filename or "").suffix.lower()
    if suffix == ".wav":
        return content_type == "audio/wav"
    return suffix in _FORMATS and content_type in _FORMATS[suffix][0]


def _input_options(source: Path) -> list[str]:
    demuxer = _FORMATS[source.suffix.lower()][1]
    options = ["-protocol_whitelist", "file", "-f", demuxer]
    if demuxer == "mov":
        options += ["-enable_drefs", "0"]
    return options


def _probe_channels(source: Path) -> int:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                *_input_options(source),
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name,channels,sample_rate",
                "-of",
                "json",
                str(source.resolve()),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=True,
        )
    except FileNotFoundError as error:
        raise ConversionUnavailableError(
            "Audio conversion is unavailable on this server"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise InvalidAudioError("Audio inspection exceeded the time limit") from error
    except subprocess.CalledProcessError as error:
        raise InvalidAudioError("Invalid or unsupported audio") from error

    try:
        stream = json.loads(result.stdout)["streams"][0]
        channels = int(stream["channels"])
        sample_rate = int(stream["sample_rate"])
        codec = stream["codec_name"]
    except (ValueError, KeyError, IndexError, TypeError) as error:
        raise InvalidAudioError("Invalid or unsupported audio") from error
    if codec not in _FORMATS[source.suffix.lower()][2]:
        raise InvalidAudioError("Audio codec does not match the file type")
    if channels not in {1, 2}:
        raise InvalidAudioError("Audio must be mono or stereo")
    if not 8000 <= sample_rate <= 96000:
        raise InvalidAudioError("Audio sample rate must be between 8000 and 96000 Hz")
    return channels


def convert_to_wav(
    source: Path,
    *,
    max_duration_seconds: int,
    max_bytes: int,
) -> Path:
    """Return an owned WAV; preserve the source and remove partial output on failure.

    The caller owns both files after success. A pre-existing destination is never
    overwritten or removed. WAV uploads bypass this function entirely.
    """
    if source.suffix.lower() not in _FORMATS:
        raise ValueError("Unsupported source format")
    if max_duration_seconds <= 0 or max_bytes <= 0:
        raise ValueError("Conversion limits must be positive")
    if not _conversion_slots.acquire(blocking=False):
        raise ConversionBusyError("Audio conversion is busy; please try again")
    try:
        channels = _probe_channels(source)
        return _decode_to_wav(
            source,
            channels=channels,
            max_duration_seconds=max_duration_seconds,
            max_bytes=max_bytes,
        )
    finally:
        _conversion_slots.release()


def _decode_to_wav(
    source: Path, *, channels: int, max_duration_seconds: int, max_bytes: int
) -> Path:
    destination = source.with_suffix(".wav")
    # Exclusive creation happens before the cleanup block: a collision is not ours.
    output = destination.open("xb+")
    process = None
    timer = None
    timed_out = Event()
    try:
        with (
            output,
            sf.SoundFile(
                output,
                mode="w",
                samplerate=TARGET_SAMPLE_RATE,
                channels=channels,
                format="WAV",
                subtype="FLOAT",
            ) as audio,
        ):
            try:
                process = subprocess.Popen(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-v",
                        "error",
                        "-xerror",
                        "-filter_threads",
                        "1",
                        "-threads",
                        "1",
                        *_input_options(source),
                        "-err_detect",
                        "explode",
                        "-i",
                        str(source.resolve()),
                        "-map",
                        "0:a:0",
                        "-vn",
                        "-sn",
                        "-dn",
                        "-threads",
                        "1",
                        "-ar",
                        str(TARGET_SAMPLE_RATE),
                        "-ac",
                        str(channels),
                        "-c:a",
                        "pcm_f32le",
                        "-f",
                        "f32le",
                        "pipe:1",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
            except FileNotFoundError as error:
                raise ConversionUnavailableError(
                    "Audio conversion is unavailable on this server"
                ) from error

            def stop_overdue_process():
                timed_out.set()
                process.kill()

            timer = Timer(CONVERSION_TIMEOUT_SECONDS, stop_overdue_process)
            timer.start()
            frames = 0
            header_bytes = output.tell()
            while block := process.stdout.read(65536):
                if len(block) % (channels * 4):
                    raise InvalidAudioError("Incomplete decoded audio")
                samples = np.frombuffer(block, dtype="<f4").reshape(-1, channels)
                frames += len(samples)
                if frames > max_duration_seconds * TARGET_SAMPLE_RATE:
                    raise AudioTooLongError(
                        "Audio exceeds the maximum allowed duration"
                    )
                if header_bytes + frames * channels * 4 > max_bytes:
                    raise ConvertedAudioTooLargeError(
                        "Converted audio exceeds the maximum allowed size"
                    )
                if not np.isfinite(samples).all():
                    raise InvalidAudioError("Audio contains non-finite samples")
                audio.write(samples)

            return_code = process.wait()
            timer.cancel()
            timer.join()
            if timed_out.is_set():
                raise InvalidAudioError("Audio conversion exceeded the time limit")
            if return_code or not frames:
                raise InvalidAudioError("Invalid or incomplete audio")
        if destination.stat().st_size > max_bytes:
            raise ConvertedAudioTooLargeError(
                "Converted audio exceeds the maximum allowed size"
            )
        return destination
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        if timer is not None:
            timer.cancel()
            timer.join()
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()
