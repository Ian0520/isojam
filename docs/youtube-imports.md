# YouTube audio importer

The importer downloads one non-live YouTube video's audio and produces a
validated WAV. The authenticated API queues durable imports, and a separate CPU
worker registers successful results as owned uploads. The browser has no URL
field yet; the standalone command remains available for independent checks.

## Background API workflow

The Compose stack starts `importer` from `Dockerfile.youtube`, sharing the data
volume with the API and GPU processor. Migrations finish before workers start;
the importer and processor must be healthy before the API starts. The importer
needs neither a GPU nor the JWT secret. Outside Compose, import admission defaults
to disabled; set `ISOJAM_YOUTUBE_IMPORTS_ENABLED=1` only when a worker is arranged.

After signing in through `/docs`, authorize with the returned Bearer token:

1. `POST /imports` with `{"url":"https://youtu.be/YE7VzlLtp-4"}` and an
   `Idempotency-Key` header, such as `youtube-demo-1`. The `202` response contains
   an import ID and `pending` status; it means acceptance, not finished audio.
2. Poll `GET /imports/{id}`. States are `pending`, `processing`, `ready` and
   `failed`. `GET /imports?limit=20` returns the latest owned imports, including
   unfinished requests after a browser reload. Other users' imports return `404`.
3. A `ready` response includes `upload_id` and a display filename. Submit that ID
   through the existing `POST /jobs` endpoint to start separation. Importing alone
   does not automatically create a separation job.

Reuse a key when retrying a lost response. Equivalent URLs for the same video
replay the existing import; reusing the key for another video returns `409`.
Failed imports also replay with the old key: use a new key for a deliberate retry.
Replays work even if admission is subsequently disabled or the queue is full.

Defaults allow one unfinished import per user and eight across the instance,
configured by `ISOJAM_MAX_UNFINISHED_IMPORTS_PER_USER` and
`ISOJAM_MAX_UNFINISHED_IMPORTS`. A full allowance returns `429`; disabled admission
returns `503`. Atomic SQLite admission handles simultaneous requests. These
allowances cover pending/processing imports, separately from separation jobs.

The worker processes one import at a time without holding a database transaction
during download or conversion. It publishes the validated WAV, then commits the
upload record and `ready` status in one database transaction. A crash between the
filesystem write and database commit can leave an orphan, which recovery removes.
Recovery preserves committed uploads, including a commit that succeeded before
the connection reported an error.

Normal SIGTERM/Ctrl+C finishes the current bounded import and leaves pending work
for startup. Compose allows three minutes for this drain. A forced interruption
marks in-progress imports `failed` with `error_code: interrupted` on restart;
they are not silently downloaded again. Queued work stays pending. A shared
filesystem lock prevents a second worker from recovering while the old publisher
or downloader is alive. The downloader inherits the lock descriptor and watches
its parent; if that parent is killed, it stops its own process group, including
Deno children. This is a single-host/local-filesystem design, not a distributed queue.
Do not remove `audio/imports/worker.lock` to bypass a running worker.

For worker diagnostics use `docker compose logs importer` and
`docker compose exec importer python -m app.import_worker health`. An unexpected
storage/database error stops the worker for inspection; after addressing the
cause, restart it with `docker compose restart importer`. Audio and import records
currently have no automatic retention policy.

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
- In the dedicated downloader child, DNS lookups are limited to YouTube hosts,
  Google Video hosts and the two required Google API endpoints. Every DNS answer
  must be public, and sockets must connect to numeric public addresses over port
  443. Redirects pass through the same checks; loopback, private, link-local and
  metadata-service destinations are rejected. This guard applies to the pinned
  Python HTTP clients; the FFmpeg stage permits only local file input.
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
There is no automatic retry. HTTP responses expose stable error codes rather
than downloader diagnostics or local filesystem paths.

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

To run the opt-in live API check after `docker compose build`:

```bash
python3 backend/scripts/smoke_youtube.py
```

This creates disposable CPU containers and a separate labeled volume, imports the
default sample video through real HTTP, verifies queued acceptance and API restart
persistence, registers a valid WAV, submits a separation job, checks graceful
importer exit and verifies the same WAV hash after another API restart. It removes
its own containers/volume afterward. It does not run inference or touch the normal
stack. Use `--video-url` for another sample; live checks depend on network/provider
availability and are intentionally outside CI.

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

The background API and worker are implemented. Browser integration is the next
slice; this document's API steps can be exercised through `/docs` in the meantime.

### Background queue verification

The completed slice passed all 1,558 backend tests with locked dependencies as
UID/GID 10001, plus Ruff lint and formatting for 123 maintained Python files.
Coverage includes concurrent admission, per-user/global limits, ownership,
idempotent replay, interrupted-work recovery, ambiguous commit results, graceful
SIGTERM drain and downloader/descendant termination after supervisor death.

The live HTTP check accepted the sample URL in 0.018 seconds, retained the pending
import through an API restart, registered the validated WAV, and accepted its
upload ID through the existing queued job endpoint. After a clean importer stop
and another API restart, the upload ID and exact WAV hash were unchanged. This
check exercised import and job admission; it did not run GPU inference.

The disposable Compose smoke also passed startup, shutdown, MP3 conversion,
queued execution and persistent replacement with the importer service included.
The normal personal-use stack and its volumes were left untouched.
