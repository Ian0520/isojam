import json
import subprocess
import sys

import pytest

from app.youtube_download import DownloadRejected, check_video, select_audio

VIDEO_ID = "BaW_jenozKc"


def test_accepts_non_live_video_at_duration_limit():
    check_video(
        {"id": VIDEO_ID, "duration": 600}, video_id=VIDEO_ID, max_duration_seconds=600
    )


@pytest.mark.parametrize(
    "fields, code",
    [
        ({"duration": 601}, "too_long"),
        ({"duration": None}, "unsupported"),
        ({"duration": float("nan")}, "unsupported"),
        ({"duration": 0}, "unsupported"),
        ({"is_live": True}, "unsupported"),
        ({"live_status": "is_upcoming"}, "unsupported"),
        ({"live_status": "post_live"}, "unsupported"),
        ({"_type": "playlist"}, "unsupported"),
        ({"id": "differentid"}, "unsupported"),
    ],
)
def test_rejects_unsupported_video_before_download(fields, code):
    with pytest.raises(DownloadRejected) as captured:
        check_video(
            {"id": VIDEO_ID, "duration": 10} | fields,
            video_id=VIDEO_ID,
            max_duration_seconds=600,
        )
    assert captured.value.code == code


def audio_format(**changes):
    return {
        "ext": "m4a",
        "vcodec": "none",
        "acodec": "mp4a.40.2",
        "protocol": "https",
        "url": "https://rr1.googlevideo.com/videoplayback?token=example",
    } | changes


def test_selects_audio_only_and_can_fall_back_to_webm():
    opus = audio_format(ext="webm", acodec="opus")
    assert list(select_audio({"formats": [opus]}, max_bytes=100)) == [opus]
    m4a = audio_format()
    assert list(select_audio({"formats": [m4a, opus]}, max_bytes=100)) == [m4a]


@pytest.mark.parametrize(
    "fields",
    [
        {"vcodec": "h264"},
        {"acodec": "none"},
        {"protocol": "m3u8_native"},
        {"url": "https://127.0.0.1/audio"},
        {"url": "http://rr1.googlevideo.com/audio"},
        {"url": "https://rr1.googlevideo.com.evil.test/audio"},
        {"url": "https://user@rr1.googlevideo.com/audio"},
        {"url": "https://rr1.googlevideo.com:443/audio"},
        {"url": "https://rr1.googlevideo.com:invalid/audio"},
        {"ext": "mp4"},
        {"ext": "webm", "acodec": "vorbis"},
        {"acodec": "mp3"},
    ],
)
def test_rejects_unexpected_streams(fields):
    with pytest.raises(DownloadRejected):
        list(select_audio({"formats": [audio_format(**fields)]}, max_bytes=100))


def test_uses_smaller_stream_or_rejects_known_oversized_download():
    large = audio_format(filesize=101)
    small = audio_format(ext="webm", acodec="opus", filesize=100)
    assert list(select_audio({"formats": [large, small]}, max_bytes=100)) == [small]
    with pytest.raises(DownloadRejected) as captured:
        list(select_audio({"formats": [large]}, max_bytes=100))
    assert captured.value.code == "too_large"


def test_kernel_file_limit_stops_download_even_without_progress_reporting(tmp_path):
    script = """
from app import youtube_download as worker
def oversized_download(video_id, directory, *, max_bytes, **limits):
    (directory / 'source.m4a.part').write_bytes(b'x' * (max_bytes + 1))
    return {'title': 'unexpected success'}
worker.download = oversized_download
raise SystemExit(worker.main())
"""
    result = subprocess.run(
        [sys.executable, "-c", script, VIDEO_ID, str(tmp_path), "1024", "1"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 1
    assert "error" in json.loads(result.stdout)
    assert (tmp_path / "source.m4a.part").stat().st_size <= 1024
