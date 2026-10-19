# YouTube audio importer

The standalone importer downloads one non-live YouTube video's audio and produces
a validated WAV. It is the first component of the URL-import workflow; it does not
create an account, upload record or separation job, and the browser has no URL field yet.

## Run the standalone component

From the repository root in Linux/WSL with Docker running:

```bash
docker build --platform linux/amd64 -f backend/Dockerfile.youtube -t isojam-youtube:local backend
docker run --rm --init \
  --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,size=128m \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --mount type=volume,src=isojam-youtube-exercise,dst=/var/lib/isojam/audio/imports \
  isojam-youtube:local python -m app.youtube \
  'https://www.youtube.com/watch?v=YE7VzlLtp-4' \
  --output-dir /var/lib/isojam/audio/imports
```

The output is JSON containing the video ID, title and generated WAV path. The WAV
remains in the `isojam-youtube-exercise` volume after the container exits. That
volume is separate from the normal Compose stack; these files will not appear in
its recording history. Each successful invocation creates a new file.

The importer image contains FFmpeg, ffprobe, yt-dlp, bundled EJS scripts and Deno.
It runs as UID/GID 10001. The API and GPU images do not gain the downloader runtime.

## Boundaries and failures

- Accepted links use HTTPS and identify a single video on `youtube.com` or
  `youtu.be`; watch, short-link, Shorts and embed forms are understood. Tracking,
  timestamp and playlist parameters are discarded when constructing the canonical
  watch URL. Playlist-only and channel links are rejected.
- Downloading runs in a dedicated child process, with a 120-second wall-clock
  timeout and 10-second socket timeout. A timed-out process and its JavaScript
  descendants are stopped together. Ambient downloader config, plugins, browser
  cookies, netrc credentials and remote component downloads are not loaded.
- Only direct HTTPS audio streams on Google Video hosts are selected, preferring
  M4A and falling back to Opus/WebM. Video streams and manifest-based downloads
  are excluded. This restriction can leave a video without an eligible stream.
- Metadata is checked before downloading. Defaults allow 10 minutes, 200 MiB of
  downloaded audio and 256 MiB of normalized WAV. `ISOJAM_MAX_AUDIO_DURATION_SECONDS`,
  `ISOJAM_MAX_UPLOAD_BYTES` and `ISOJAM_MAX_CONVERTED_AUDIO_BYTES` configure those
  limits. Actual downloaded bytes also have a kernel file-size limit; actual
  decoded frames and WAV size are checked independently of metadata.
- Downloads and conversion use a private temporary directory. The final WAV is
  linked into the output directory only after validation, without overwriting an
  existing file. Failed imports remove their temporary files.

The command exits nonzero with a structured error when a video is unavailable,
has no eligible audio stream, exceeds a limit, times out or cannot be decoded.
Download availability depends on YouTube and the selected downloader version.
There is no automatic retry or public HTTP endpoint in this slice.

## Dependency maintenance and checks

`requirements-youtube.in` declares the importer additions, constrained by the API
lock. `requirements-youtube.txt` pins versions and artifact hashes. Regenerate it
with the project's existing CPython 3.12/Linux x86_64 tooling environment:

```bash
cd backend
python scripts/compile_requirements.py --youtube
```

Use `--youtube --upgrade` for a deliberate update. This does not regenerate the API
or GPU locks. CI builds the importer image and checks its packaged dependencies
without network access; unit tests use generated audio and controlled downloader
responses. Live downloads remain a separate manual integration check.

## Verification recorded on 2026-10-20

The image downloaded `YE7VzlLtp-4` (*Big Buck Bunny*) and produced one FLOAT WAV at
44.1 kHz, stereo, 596.5206 seconds and 210,452,568 bytes. Download, conversion and
validation took 3.50 seconds on the development host. The check ran as UID 10001
with a read-only root filesystem and disposable tmpfs; no compressed source or
temporary cache remained. A different unavailable video returned a clean failure.
These checks establish import behavior, rather than throughput or separation quality.

All 160 focused importer, conversion, audio-validation, upload and browser-API tests
passed with locked dependencies as UID/GID 10001. Ruff lint and formatting passed
for all 115 maintained Python files. The importer lock regenerated identically;
the API and GPU locks were unchanged. Offline container checks confirmed the
packaged downloader, EJS scripts, Deno, FFmpeg and ffprobe.

The next slice connects this component to durable background imports, ownership,
admission limits and restart handling. Browser integration follows that API work.
