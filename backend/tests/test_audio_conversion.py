import subprocess
import sys
from threading import BoundedSemaphore

import numpy as np
import pytest
import soundfile as sf

import app.audio_conversion as conversion
from app.audio import AudioTooLongError, InvalidAudioError, validate_wav
from app.audio_conversion import (
    ConversionBusyError,
    ConversionUnavailableError,
    ConvertedAudioTooLargeError,
    convert_to_wav,
    get_audio_upload_types,
    supports_audio_upload,
)


def test_internal_webm_support_is_not_advertised_or_accepted_as_an_upload():
    assert ".webm" not in get_audio_upload_types()
    assert not supports_audio_upload("recording.webm", "audio/webm")


@pytest.mark.parametrize("suffix", [".mp3", ".m4a", ".aac", ".flac"])
@pytest.mark.parametrize("channels", [1, 2])
def test_converts_real_audio_to_valid_float_wav(encoded_audio, suffix, channels):
    source = encoded_audio(suffix, channels=channels, sample_rate=48000)
    original = source.read_bytes()
    output = convert_to_wav(source, max_duration_seconds=1, max_bytes=100000)

    assert output == source.with_suffix(".wav")
    assert source.read_bytes() == original
    validate_wav(output, max_duration_seconds=1)
    with sf.SoundFile(output) as audio:
        assert audio.samplerate == 44100
        assert audio.channels == channels
        assert audio.subtype == "FLOAT"
        assert audio.frames > 0
        samples = audio.read()
        assert np.isfinite(samples).all()
        assert np.max(np.abs(samples)) > 0.05


def test_accepts_alac_in_m4a_container(encoded_audio):
    source = encoded_audio(".m4a", codec="alac")
    output = convert_to_wav(source, max_duration_seconds=1, max_bytes=100000)
    validate_wav(output, max_duration_seconds=1)


@pytest.mark.parametrize("frames, accepted", [(44100, True), (44101, False)])
def test_bounds_decoded_duration_without_truncating(encoded_audio, frames, accepted):
    source = encoded_audio(".flac", frames=frames)
    if accepted:
        output = convert_to_wav(source, max_duration_seconds=1, max_bytes=400000)
        assert sf.info(output).frames == frames
    else:
        with pytest.raises(AudioTooLongError):
            convert_to_wav(source, max_duration_seconds=1, max_bytes=400000)
        assert not source.with_suffix(".wav").exists()
        assert source.exists()


def test_bounds_expanded_file_size(encoded_audio):
    source = encoded_audio(".flac", frames=44100)
    assert source.stat().st_size < 100000
    with pytest.raises(ConvertedAudioTooLargeError):
        convert_to_wav(source, max_duration_seconds=1, max_bytes=100000)
    assert not source.with_suffix(".wav").exists()


def test_expanded_size_accepts_exact_limit_and_rejects_one_byte_over(encoded_audio):
    source = encoded_audio(".flac")
    output = convert_to_wav(source, max_duration_seconds=1, max_bytes=100000)
    exact_size = output.stat().st_size
    output.unlink()
    output = convert_to_wav(source, max_duration_seconds=1, max_bytes=exact_size)
    assert output.stat().st_size == exact_size
    output.unlink()
    with pytest.raises(ConvertedAudioTooLargeError):
        convert_to_wav(source, max_duration_seconds=1, max_bytes=exact_size - 1)
    assert not output.exists()


def test_preserves_existing_destination(encoded_audio):
    source = encoded_audio()
    destination = source.with_suffix(".wav")
    destination.write_bytes(b"an existing recording")
    with pytest.raises(FileExistsError):
        convert_to_wav(source, max_duration_seconds=1, max_bytes=100000)
    assert destination.read_bytes() == b"an existing recording"
    assert source.exists()


def test_rejects_multichannel_audio(encoded_audio):
    source = encoded_audio(".flac", channels=6)
    with pytest.raises(InvalidAudioError, match="mono or stereo"):
        convert_to_wav(source, max_duration_seconds=1, max_bytes=100000)
    assert not source.with_suffix(".wav").exists()


def test_rejects_invalid_contents(encoded_audio):
    source = encoded_audio()
    source.write_bytes(b"not an MP3")
    with pytest.raises(InvalidAudioError):
        convert_to_wav(source, max_duration_seconds=1, max_bytes=100000)
    assert not source.with_suffix(".wav").exists()


def test_missing_probe_is_service_unavailable(tmp_path, monkeypatch):
    source = tmp_path / "source.mp3"
    source.touch()

    def missing_probe(*args, **kwargs):
        raise FileNotFoundError("ffprobe")

    monkeypatch.setattr(conversion.subprocess, "run", missing_probe)
    with pytest.raises(ConversionUnavailableError):
        convert_to_wav(source, max_duration_seconds=1, max_bytes=100000)
    assert not source.with_suffix(".wav").exists()


def test_probe_timeout_releases_conversion_slot(tmp_path, monkeypatch):
    source = tmp_path / "source.mp3"
    source.touch()
    slots = BoundedSemaphore(1)
    monkeypatch.setattr(conversion, "_conversion_slots", slots)

    def slow_probe(*args, **kwargs):
        raise subprocess.TimeoutExpired("ffprobe", timeout=5)

    monkeypatch.setattr(conversion.subprocess, "run", slow_probe)
    with pytest.raises(InvalidAudioError, match="time limit"):
        convert_to_wav(source, max_duration_seconds=1, max_bytes=100000)
    assert slots.acquire(blocking=False)
    slots.release()
    assert not source.with_suffix(".wav").exists()


def test_rejects_unapproved_codec_in_m4a_container(encoded_audio):
    original = encoded_audio(".flac")
    source = original.with_suffix(".m4a")
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(original),
            "-c:a",
            "pcm_s16le",
            "-f",
            "mov",
            str(source),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    with pytest.raises(InvalidAudioError, match="codec does not match"):
        convert_to_wav(source, max_duration_seconds=1, max_bytes=100000)
    assert not source.with_suffix(".wav").exists()


def test_rejects_video_without_audio(encoded_audio):
    original = encoded_audio(".m4a")
    source = original.with_name("video.m4a")
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=16x16:d=0.04",
            "-an",
            "-c:v",
            "mpeg4",
            "-f",
            "mov",
            str(source),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    with pytest.raises(InvalidAudioError):
        convert_to_wav(source, max_duration_seconds=1, max_bytes=100000)
    assert not source.with_suffix(".wav").exists()


def test_rejects_work_when_conversion_slots_are_full(tmp_path, monkeypatch):
    slots = BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(conversion, "_conversion_slots", slots)
    source = tmp_path / "source.mp3"
    with pytest.raises(ConversionBusyError):
        convert_to_wav(source, max_duration_seconds=1, max_bytes=100000)
    assert list(tmp_path.iterdir()) == []


@pytest.fixture
def decoder_process(tmp_path, monkeypatch):
    """Use real child processes to exercise blocked reads and failed decoders."""
    source = tmp_path / "source.mp3"
    source.write_bytes(b"source")
    monkeypatch.setattr(conversion, "_probe_channels", lambda path: 1)
    original_popen = subprocess.Popen
    children = []

    def run_script(script):
        def start_decoder(command, **kwargs):
            child = original_popen([sys.executable, "-c", script], **kwargs)
            children.append(child)
            return child

        monkeypatch.setattr(conversion.subprocess, "Popen", start_decoder)
        return source

    yield run_script
    assert all(child.poll() is not None for child in children)


def test_timeout_kills_blocked_decoder_and_cleans_output(decoder_process, monkeypatch):
    source = decoder_process("import time; time.sleep(60)")
    monkeypatch.setattr(conversion, "CONVERSION_TIMEOUT_SECONDS", 0.1)
    with pytest.raises(InvalidAudioError, match="time limit"):
        convert_to_wav(source, max_duration_seconds=1, max_bytes=100000)
    assert not source.with_suffix(".wav").exists()
    assert source.exists()


@pytest.mark.parametrize(
    "script",
    [
        "import sys; sys.exit(1)",
        "import struct, sys; sys.stdout.buffer.write(struct.pack('<f', 0.5)); sys.exit(1)",
        "import sys; sys.stdout.buffer.write(b'bad')",
        "import struct, sys; sys.stdout.buffer.write(struct.pack('<f', float('nan')))",
    ],
)
def test_failed_or_invalid_decoder_output_is_removed(decoder_process, script):
    source = decoder_process(script)
    with pytest.raises(InvalidAudioError):
        convert_to_wav(source, max_duration_seconds=1, max_bytes=100000)
    assert not source.with_suffix(".wav").exists()


def test_missing_decoder_cleans_output(tmp_path, monkeypatch):
    source = tmp_path / "source.mp3"
    source.touch()
    monkeypatch.setattr(conversion, "_probe_channels", lambda path: 1)

    def missing_decoder(*args, **kwargs):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(conversion.subprocess, "Popen", missing_decoder)
    with pytest.raises(ConversionUnavailableError):
        convert_to_wav(source, max_duration_seconds=1, max_bytes=100000)
    assert not source.with_suffix(".wav").exists()
