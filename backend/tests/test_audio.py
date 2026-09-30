import numpy as np
import pytest
import soundfile as sf

from app.audio import AudioTooLongError, InvalidAudioError, validate_wav
from tests.factories import make_wav_bytes


@pytest.mark.parametrize("channels", [1, 2])
@pytest.mark.parametrize("sample_rate", [8000, 44100, 48000, 96000])
def test_validate_wav_accepts_supported_channels_and_sample_rates(
    tmp_path, channels, sample_rate
):
    path = tmp_path / "test.wav"
    path.write_bytes(make_wav_bytes(channels=channels, sample_rate=sample_rate))
    validate_wav(path, max_duration_seconds=1)


@pytest.mark.parametrize(
    ("format", "subtype"),
    [
        ("WAV", "PCM_16"),
        ("WAV", "PCM_24"),
        ("WAV", "PCM_32"),
        ("WAV", "FLOAT"),
        ("WAV", "DOUBLE"),
        ("WAVEX", "PCM_16"),
    ],
)
def test_validate_wav_accepts_pcm_and_floating_point_audio(tmp_path, format, subtype):
    path = tmp_path / "test.wav"
    sf.write(path, np.zeros((16, 2)), 44100, format=format, subtype=subtype)
    validate_wav(path, max_duration_seconds=1)


@pytest.mark.parametrize("payload", [b"", b"not audio", make_wav_bytes()[:30]])
def test_validate_wav_rejects_malformed_files(tmp_path, payload):
    path = tmp_path / "test.wav"
    path.write_bytes(payload)
    with pytest.raises(InvalidAudioError, match="Invalid or unsupported WAV audio"):
        validate_wav(path, max_duration_seconds=1)


def test_validate_wav_rejects_another_format_with_wav_filename(tmp_path):
    path = tmp_path / "test.wav"
    sf.write(path, np.zeros((16, 2)), 44100, format="FLAC")
    with pytest.raises(InvalidAudioError, match="Invalid or unsupported WAV audio"):
        validate_wav(path, max_duration_seconds=1)


def test_validate_wav_rejects_empty_audio(tmp_path):
    path = tmp_path / "test.wav"
    path.write_bytes(make_wav_bytes(frames=0))
    with pytest.raises(InvalidAudioError):
        validate_wav(path, max_duration_seconds=1)


@pytest.mark.parametrize("channels", [3, 6])
def test_validate_wav_rejects_multichannel_audio(tmp_path, channels):
    path = tmp_path / "test.wav"
    path.write_bytes(make_wav_bytes(channels=channels))
    with pytest.raises(InvalidAudioError, match="mono or stereo"):
        validate_wav(path, max_duration_seconds=1)


@pytest.mark.parametrize("sample_rate", [7999, 96001])
def test_validate_wav_rejects_sample_rates_outside_allowed_range(tmp_path, sample_rate):
    path = tmp_path / "test.wav"
    path.write_bytes(make_wav_bytes(sample_rate=sample_rate))
    with pytest.raises(InvalidAudioError, match="sample rate"):
        validate_wav(path, max_duration_seconds=1)


def test_validate_wav_accepts_exact_duration_limit(tmp_path):
    path = tmp_path / "test.wav"
    path.write_bytes(make_wav_bytes(frames=8000, sample_rate=8000))
    validate_wav(path, max_duration_seconds=1)


def test_validate_wav_rejects_one_frame_over_duration_limit(tmp_path):
    path = tmp_path / "test.wav"
    path.write_bytes(make_wav_bytes(frames=8001, sample_rate=8000))
    with pytest.raises(AudioTooLongError, match="maximum allowed duration"):
        validate_wav(path, max_duration_seconds=1)


@pytest.mark.parametrize("sample", [float("nan"), float("inf"), -float("inf")])
def test_validate_wav_rejects_non_finite_samples_late_in_file(tmp_path, sample):
    path = tmp_path / "test.wav"
    samples = np.zeros((70000, 2), dtype="float32")
    samples[-1, 0] = sample
    sf.write(path, samples, 44100, subtype="FLOAT")
    with pytest.raises(InvalidAudioError, match="non-finite samples"):
        validate_wav(path, max_duration_seconds=2)
