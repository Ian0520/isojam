# Configuration and API reference

[Back to the project overview](../README.md). For everyday local use, follow the
[Docker Compose quickstart](../README.md#start-locally-with-docker-compose).
This document covers configuration, API behavior and the alternative native
Python setup. Compose already supplies its container dependencies, storage paths,
migrations and processor; you do not need the native installation below to use it.

## Setup

This section starts the application directly in a Python environment on
Ubuntu/WSL2. Its environment variables and data are separate from Compose.
The root `.env` is read by Compose; native Python commands do not load it
automatically.

### Prerequisites

- Python 3.12 for the tested runtime
- Git
- FFmpeg and ffprobe for MP3, M4A, AAC and FLAC uploads; the CPU container supplies
  both. For native Ubuntu/WSL use `sudo apt-get update` and
  `sudo apt-get install --no-install-recommends ffmpeg`. WAV uploads work without them.
- NVIDIA GPU and a compatible CUDA-enabled PyTorch environment for GPU inference

### Install Dependencies

Clone the repository and create a virtual environment:

```bash
git clone https://github.com/Ian0520/isojam.git
cd isojam/backend

python3 -m venv .venv
source .venv/bin/activate
```

For local GPU processing, install the backend with the inference dependencies:

```bash
python -m pip install -e ".[inference]"
```

For API-only operation on Python 3.12 / Linux x86_64, use the tested dependency
locks in a fresh environment:

```bash
python -m pip install pip==26.2.1
python -m pip install --require-hashes --only-binary=:all: -r requirements-api.txt
python -m pip install --no-deps --no-build-isolation .
python -m pip check
```

Then select disabled or queued processing mode as described below. The editable
inference extra supplies the model package and its dependencies; it does not
reproduce the hash-locked GPU image. The CPU locks cover only the API environment.
For the separate GPU runtime/build locks, see
[GPU dependency maintenance](local-stack.md#gpu-image-and-dependency-maintenance).
See [Python runtime](python-runtime.md) for the installation flags and targets.

Dependencies are declared in `backend/pyproject.toml`. An editable install (`-e`)
links the environment to the source tree during development. The API deployment
command installs a built copy of the checked-out source.

The optional inference dependency is pinned to an upstream commit that provides
the programmatic `BSRoformerSession` API.

### Initialize the Database

Metadata defaults to the project-level `data/isojam.db`. To use a database file
on a persistent disk, set an absolute file path before running migrations or
starting the server:

```bash
export ISOJAM_DATABASE_PATH=/var/lib/isojam/metadata/isojam.db
```

The application and Alembic use this same setting. Parent directories are created
when initializing the application database engine or running online migrations.
Blank/relative paths, existing directories, and file-valued parents are rejected.
The application reads the path when its database module loads; restart the server
to apply a change. Setting a new path does not move or copy an existing database.

From the `backend` directory, initialize or upgrade the selected database:

```bash
alembic upgrade head
```

Migrations create/update tables; configuring a path or creating an engine does
not create the schema. Apply migrations before starting an updated application.
`ALEMBIC_DATABASE_URL` remains an explicit migration-only override for tests or
administrative use. Leave it unset for normal deployment so migrations and the
application target the same database.

The ownership migration assumes there are no existing uploads without owners. Migrating an older database containing such uploads requires a separate data-migration plan.

### Configure Audio Storage

By default, uploaded and generated audio uses the project-level `data/uploads`
and `data/outputs` directories. To use a persistent disk outside the checkout, set
an absolute directory path before starting the server:

```bash
export ISOJAM_AUDIO_STORAGE_DIR=/var/lib/isojam/audio
```

Uploads then use `/var/lib/isojam/audio/uploads`; generated outputs use
`/var/lib/isojam/audio/outputs/<job-id>`. Directories are created when files are
saved. Blank/relative paths and paths that already refer to a file fail startup.
The setting is read when the storage module loads; restart the server to apply a
change. It does not depend on the server's working directory.

This setting controls audio storage. Configure SQLite metadata separately through
`ISOJAM_DATABASE_PATH`; both locations must be on persistent storage for deployment. Relocating an
existing installation requires a file and metadata migration: changing this
setting does not move files, and existing output records contain absolute paths.

### Configure Authentication

Generate a random signing secret once:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Keep that value securely outside the repository and set it in the shell used to start the application:

```bash
export ISOJAM_JWT_SECRET_KEY='replace-with-your-generated-secret'
```

Reuse the same secret across restarts. Changing it invalidates previously issued access tokens.

Startup fails if the environment variable is missing or blank.

### Configure Registration

`ISOJAM_REGISTRATION_MODE` accepts `closed` (the default), `open`, or `invite`. It is independent
of processing mode: closing registration does not disable login or work submitted
by existing accounts. Invalid values, including blank values, fail startup before
the model loads.

For local account creation, explicitly open registration before starting the API:

```bash
export ISOJAM_REGISTRATION_MODE=open
```

With `closed`, valid `POST /register` requests return `403` with
`{"detail":"Registration is closed"}` before email lookup, password hashing or
user creation. Existing and unknown emails receive the same response. Request
validation and body-size limits still apply, so malformed or oversized requests
can return `422` or `413` before the endpoint runs.

The mode is read once during application startup. Restart the server to apply a
change; replace a Docker container with the updated runtime setting. Closing
registration does not remove users or invalidate their tokens.

With `invite`, `POST /register` requires a valid `invitation_code` belonging to the
normalized email. Missing, incorrect, expired, used and email-mismatched codes
return the same `403`, `{"detail":"Invitation is invalid or unavailable"}`. A valid
code whose email already has an account receives that same response. Codes are
case-sensitive and must be supplied exactly as issued. `open` requires no code
and does not consume a supplied invitation; `closed` rejects it regardless.

Claiming the invitation and creating the account commit in one transaction.
The API rechecks expiry and unused state under SQLite's write lock, after password
hashing and after any lock wait. A rolled-back transaction leaves both changes
undone. The browser shows a required invitation field only in `invite` mode,
omits it in `open` mode and hides account creation in `closed` mode. It reads the
policy from `/ui-info`; reload after changing the server's mode. Successful
registration clears the code and signs in automatically. If that login fails,
retry through sign-in with the created account. See
[invitation operation and redemption](invitations.md). Email verification and
account revocation remain later work.

### Configure Upload Limits

Uploaded files are limited to 200 MiB by default. Set a different positive byte
limit in the shell used to start the application:

```bash
export ISOJAM_MAX_UPLOAD_BYTES=104857600
export ISOJAM_MAX_AUDIO_DURATION_SECONDS=300
export ISOJAM_MAX_CONVERTED_AUDIO_BYTES=134217728
```

This example sets a 100 MiB upload limit, a 5-minute duration limit and a 128 MiB
converted-file limit. Defaults are 200 MiB, 10 minutes and 256 MiB respectively.
All settings must be positive integers; invalid
configuration fails startup before the model loads.

Uploads accept WAV, MP3, M4A (AAC/ALAC), raw AAC and FLAC. Inputs must have nonempty,
readable mono or stereo audio at 8-96 kHz. WAV/WAVEX PCM and floating-point uploads
are preserved byte-for-byte. Additional formats are inspected with ffprobe and
decoded with FFmpeg into 44.1 kHz floating-point WAV, retaining mono/stereo channels.
Conversion cannot restore information lost in a lossy recording.

Duration, converted size and finite samples are checked against actual decoded
audio through bounded blocks. Conversion uses local files only, has a 5-second
inspection timeout and a 30-second decoding timeout, and allows two simultaneous
conversions per API process. Invalid audio returns `422`; upload, duration or
converted-size violations return `413`. Busy conversion or missing FFmpeg/ffprobe
returns `503`. Unsupported filename/content-type combinations return `415`.
Rejected requests remove their temporary files without creating upload metadata.
The original compressed file is removed after successful conversion and validation;
the upload record keeps its display filename and points to the stored WAV.

Request-size middleware also limits `POST /uploads` to the configured file
limit plus 64 KiB for multipart headers and form fields. Other API requests have
a 16 KiB body limit. Oversized requests return `413`. The middleware checks both
`Content-Length` and actual received bytes before passing body chunks to the
request parser, so missing or understated lengths cannot bypass the limit.
Multipart temporary files are closed when parsing is interrupted.

The endpoint still enforces the audio file's own byte limit and validates its
contents and duration; multipart overhead does not increase the allowed file size.

### Configure Processing Mode

`ISOJAM_PROCESSING_MODE` accepts `local` (the default), `disabled`, or `queued`.
Invalid values fail startup before model loading.

Local mode requires the inference extra and a compatible CUDA environment. The
model loads once during startup and closes during shutdown. A missing inference
package produces an installation message; model startup failures fail the server
rather than silently switching modes.

For API-only operation on a CPU host:

```bash
export ISOJAM_PROCESSING_MODE=disabled
```

Health, login, uploads, job status and existing output downloads remain available;
registration follows its separate policy. New processing jobs return `503` without
creating a job record or scheduling work. This mode does not perform CPU inference or submit jobs to an
external worker; worker integration is still required for a complete hosted flow.

To accept durable work without loading a model:

```bash
export ISOJAM_PROCESSING_MODE=queued
```

Queued mode commits a pending job with `execution_backend="queued"` and returns
its ID without scheduling a background task. Jobs and optional idempotency
receipts commit together, and remain available after API replacement. Ownership,
authentication, and the unfinished-job allowance still apply. Replaying a receipt
returns the original job even if the API's processing mode has changed; it does
not change that job's backend or schedule it again.

Queued jobs remain pending until a dispatcher reserves and submits them. Use
`--adapter local-model` for real inference in the same-host CUDA environment, or
the explicit `local-fake` adapter for dummy WAVs in CPU-only checks.
See [Real model worker](model-worker.md) for setup and failure behavior.
Pending/processing jobs count
toward the allowance and cannot serve outputs (409); completed jobs expose owned
downloads. Publication recovery for confirmed stopped workers is implemented;
uncertain executions remain occupied. Use `--serve --adapter local-model` for
continuous same-host processing; see the worker guide for graceful shutdown and
the supervisor example. Remote storage/control and hosted deployment remain
separate work.

### Local dispatcher coordination exercise

After applying migrations, a dispatcher can run separately from the API. From
`backend`, with the same absolute `ISOJAM_DATABASE_PATH` and
`ISOJAM_AUDIO_STORAGE_DIR` as the queued API:

```bash
python -m app.dispatcher --once --adapter local-fake
```

This explicitly runs one cycle. It first tries to recover one unfinished stopped
attempt from saved results. If none qualifies, it commits reservation and submission
intent for one new job, then starts `python -m app.fake_worker` as a separate child.
The fake worker reads its validated invocation from standard input, obtains permission and records one
heartbeat, then generates seven tiny deterministic WAVs in a private workspace.
It saves a verified bundle under `ISOJAM_AUDIO_STORAGE_DIR/results`. These are
explicitly fake artifacts, not separated audio. Neither command starts FastAPI
or loads the inference model. The local adapter uses the shared SQLite file on
the same host; it is not remote GPU control.

The dispatcher prints one JSON report. `completed` includes a `manifest_key` and
means the local worker exited successfully, the controller committed its stop
evidence, the fake bundle passed verification, and its output records and
completion committed together.
The manifest records its job/attempt/invocation identity, fake processing profile,
complete stem set, canonical storage keys, byte sizes, and SHA-256 hashes. Files
are installed without replacement; the manifest is installed last. Missing,
changed, invalid or unsafe files cannot pass verification.

Publication checks the current attempt, dispatcher owner/generation and invocation
inside a short SQLite transaction. All seven output rows, the selected manifest
key/hash, the succeeded attempt and completed job commit together. The owner can
then download the dummy WAVs through the existing output routes. Identical repeated
publication acknowledges the saved result without rewriting rows or timestamps.
The worker's `result_ready` report alone cannot complete a job. First publication
also requires durable controller-confirmed `execution_stopped_at` and the actual
`execution_exit_code`. The local worker saves its launch UUID and PID when
receiving permission; the controller must wait for that exact child and match
both with the current job/attempt/invocation and dispatcher authority. A denied duplicate child cannot
prove the original execution stopped. Exit status is diagnostic; only verification
establishes that a complete usable bundle exists. Heartbeats stop being accepted
after confirmed exit.

`recovered` means a previously unfinished stopped attempt passed bundle verification
and its publication committed (or was identically acknowledged). Recovery preserves
the original job, attempt, invocation, launch identity and execution ownership; it
repairs publication without another execution grant. Each cycle finishes one
recovery or one new delivery. To recover without dispatching pending work:

```bash
python -m app.dispatcher --once --adapter local-fake --reconcile-only
```

`idle` means no eligible work or occupied capacity. With --reconcile-only, it means
no eligible unfinished stopped attempt, even if new jobs are pending or an unknown
attempt remains occupied. Successful publication releases
the global slot for another pending queued job. With no pending jobs, a second
cycle is idle.
Use disposable data for this intermediate exercise. For a fully isolated container
exercise that creates and removes its own volume, see [Containers](containers.md).

Launch/report errors print `submission_unresolved` and preserve existing attempt
state. Failed publication leaves the files available and the attempt occupied;
`publication_unresolved`, `publication_denied`, `publication_conflict` and
`result_invalid` never authorize another execution. A database lock wait returns
`database_busy` (exit 75). Publication can be retried separately once durable
stop proof and a verified bundle are available; a subsequent dispatch cycle can
recover one such unfinished attempt. Invalid/unresolved candidate outcomes end the
cycle and preserve occupied state. A timeout kills and waits for the direct fake
child, records confirmed exit when it matches the authorized process, and retains the occupied attempt.
A crash before committing that proof leaves termination unknown, even if files
exist. Migrating older rows never invents exit proof. Existing identical completed
publications remain acknowledgeable without adding historical stop evidence.
Reconciliation can now finalize a confirmed nonzero exit with no manifest as
`failed`, releasing capacity without repeating inference. Existing invalid
manifests and unreadable storage retain their occupied state. This rule also
applies to the fake transport on a later recovery cycle.
There is no automatic inference retry or timeout-based release of uncertain work.
Continuous polling is available with `--serve`. An expired owned reservation can
be reclaimed only before submission intent, preserving its attempt and increasing
its generation. Recovery uses a stored authority snapshot and rechecks it
after verification; it never transfers execution ownership. CLI defaults are
60 seconds for pre-submission ownership, 300 for receiving execution permission, 30 for the local
fake-child wait, and 1000 milliseconds for each SQLite lock wait. The child timeout
is specific to this fake transport; it is not a GPU execution-time limit.

## Running

From `backend`, with the virtual environment activated and the signing secret configured:

```bash
uvicorn app.main:app --reload
```

Open the browser studio at [http://127.0.0.1:8000/](http://127.0.0.1:8000/).
It uses the same API and bearer authentication as other clients.

Interactive API documentation is available at:

[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

Apply pending migrations with `alembic upgrade head` before starting an updated application.

## Running the CPU API in Docker

The API image uses the locked Python dependencies and disabled processing mode.
It accepts authentication and uploads; new processing jobs return `503` until a
worker is connected. Secrets are supplied at runtime, and a named volume keeps
SQLite and audio across container replacement.

See [Container Guide](containers.md) for Docker setup, building the image,
configuration, migrations, startup, replacement, and the repeatable smoke check.

## Usage

```text
Register → Log in → Upload audio → Create job → Poll status → Download stems
```

### 1. Register and Log In

With registration explicitly open, create an account with `POST /register`, then
authenticate with `POST /login`. Existing accounts can log in while registration
is closed. Both endpoints accept JSON:

```json
{
  "email": "user@example.com",
  "password": "your-password"
}
```

Registration returns the user's ID and email. Login returns:

```json
{
  "access_token": "...",
  "token_type": "bearer"
}
```

Include the token in subsequent upload, job, and download requests:

```http
Authorization: Bearer <access_token>
```

Access tokens expire after 30 minutes by default. Log in again to obtain a new token.

### 2. Upload an Audio File

Send `POST /uploads` as multipart form data, using `audio_file` as the file field.

Use the matching filename extension and multipart content type:

| Extension | Canonical content type |
| --- | --- |
| `.wav` | `audio/wav` |
| `.mp3` | `audio/mpeg` |
| `.m4a` | `audio/mp4` |
| `.aac` | `audio/aac` |
| `.flac` | `audio/flac` |

The browser gets these mappings from `/ui-info`, checks the filename/size and sends
the canonical type even if the operating system reports an empty or different MIME
type. The API still verifies the actual audio content. The response contains the
upload ID, original filename and submitted content type; stored audio is always WAV.
An upload does not create a separation job. URL imports are not supported.

### 3. Create a Processing Job

Send `POST /jobs` with the upload ID:

```json
{
  "upload_id": "<upload-id>"
}
```

The upload must belong to the authenticated user. The response contains a job ID.

For retry-safe submission, include an optional header:

```http
Idempotency-Key: <new-unique-key-for-this-submission>
```

Generate the key before sending the request, and reuse it if the response is lost
or the request must be retried. A UUID is suitable. Keys are case-sensitive, scoped
to the authenticated user, and accept 1-128 ASCII letters, digits, `.`, `_`, `:`,
and `-`; invalid keys return `422`.

The same key and upload return the original job ID with its current status and
output URLs, without creating or scheduling another job. A replay still succeeds
when the user's unfinished-job allowance is full or processing has been disabled.
Reusing a key for a different owned upload returns `409`. Use a new key to request
a separate job, even for the same upload. Omitting the header keeps the original
behavior: each successful submission creates a new job.

Receipts are persisted without automatic expiry. Job and receipt commit together;
replaying an accepted submission does not recover interrupted background work.
The queued runner can recover publication from a confirmed stopped worker
and verified saved bundle. It does not resume local background inference. Downgrading the receipt migration
keeps jobs/results but removes stored keys and therefore their replay protection.

Each user can have at most two unfinished jobs by default, counting both `pending`
and `processing` jobs across all their uploads. At the limit, submission returns
`429` without creating or dispatching a job. Completed and failed jobs do not count.
Set a different positive limit before starting the application:

```bash
export ISOJAM_MAX_UNFINISHED_JOBS_PER_USER=3
```

Invalid configuration fails startup before the model loads. Job admission combines
the count and conditional insert in one statement and relies on SQLite's serialized
writes to protect simultaneous submissions. A different database backend requires
reviewing its locking/isolation behavior. This allowance bounds unfinished work;
it is not a daily usage limit or a global GPU concurrency limit.

### 4. View Job History

Request `GET /jobs` with bearer authentication to list the current account's jobs.
Each item contains `id`, `upload_id`, `filename` (the original upload filename),
`status`, and a UTC `created_at` timestamp. Local and queued jobs in every public
status are included. Select an item and use `GET /jobs/{job_id}` for its output
URLs.

Pages default to 20 items. Set `limit` from 1 to 100; invalid sizes return `422`.
Jobs are ordered by creation time descending, then job ID descending to resolve
equal timestamps. The response contains `items` and `next_cursor`; an account with
no jobs receives `{"items": [], "next_cursor": null}`.

When `next_cursor` is non-null, pass it unchanged in the next request:

```http
GET /jobs?limit=20&cursor=<next_cursor>
Authorization: Bearer <access_token>
```

The cursor marks the last returned creation-time/ID pair. Newly inserted jobs do
not shift subsequent pages; refresh the first page to see new jobs. Each request
reads current job statuses rather than a frozen snapshot of the entire history.
Malformed cursors return `422`. A cursor is a position, not a credential: every
page filters ownership using the authenticated account. History remains available
when new processing is disabled.

The browser's **Recent recordings** panel loads the first page after sign-in or a
signed-in reload. Select a recording to open its existing job status and outputs.
**Load more** appends the next page; **Refresh** replaces the list with the newest
page. A failed request preserves the displayed list and cursor so it can be
retried. A successful new submission refreshes history, and status polling updates
the selected job's badge. Other rows update on refresh. Sign-out clears the list
and invalidates pending requests. The **Find by job ID** section remains
available as an expandable fallback.

### 5. Check Processing Status

Poll `GET /jobs/{job_id}`.

Jobs have one of four statuses: `pending`, `processing`, `completed`, or `failed`. A completed job includes download URLs:

```json
{
  "id": "<job-id>",
  "status": "completed",
  "upload_id": "<upload-id>",
  "outputs": {
    "guitar": "/jobs/<job-id>/outputs/guitar",
    "vocals": "/jobs/<job-id>/outputs/vocals"
  }
}
```

### 6. Download a Stem

Request `GET /jobs/{job_id}/outputs/{stem}` with the same bearer authentication.

Downloads are available when the job is completed. Missing resources and resources owned by another user return 404. Missing or invalid authentication returns 401.

## Testing

Tests use temporary databases and storage, fake model sessions, and test signing secrets.

From `backend`, use a fresh Python 3.12 / Linux x86_64 environment as described
in [Python Runtime](python-runtime.md), then install the test dependencies:

```bash
python -m pip install --require-hashes --only-binary=:all: -r requirements-dev.txt
python -m pip install --no-deps --no-build-isolation -e .
python -m pip check
```

Run the full suite:

```bash
python -m pytest
```

Coverage includes registration, login, token validation, ownership enforcement, migrations, persistence, storage, processing orchestration, model integration boundaries, and output downloads.

## Further Documentation

- [Container Guide](containers.md)
- [Python Runtime](python-runtime.md)
- [Project Scope](project-scope.md)
- [Model Compatibility Spike](model-spike.md)
