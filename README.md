# IsoJam

IsoJam turns songs into practice tracks. Musicians can upload a song, separate it into instrument stems, and download isolated tracks or backing tracks for practice.

This is a backend-focused side project built with FastAPI, SQLAlchemy, and BS-RoFormer.

## Features

- Register and log in with email and password
- Authenticate requests using short-lived JWT access tokens
- Upload WAV audio files
- Create local background source-separation jobs or persist queued jobs
- Retrieve processing status and download generated stems
- Persist users, uploads, jobs, and output metadata in SQLite
- Restrict uploads, jobs, and downloads to their owners

The current model produces vocals, drums, bass, guitar, piano, other, and instrumental outputs.

## Current Limitations

- Source separation supports WAV input only
- Audio files are stored on the local filesystem
- Metadata is stored in SQLite
- Local processing uses in-process FastAPI background tasks
- Queued mode has a one-cycle local fake dispatcher/worker; queued inference and result publication are not implemented yet
- Interrupted jobs are not automatically resumed after a restart
- Authentication uses access tokens only; refresh tokens are not implemented

## Architecture

```text
Client
  ↓
FastAPI — authentication and ownership checks
  ├── SQLAlchemy repositories → SQLite metadata
  └── Background task → BS-RoFormer → local audio files
```

In local processing mode, the application loads one model session during startup
and reuses it across processing jobs. Disabled processing mode starts the API
without importing the inference package or loading a model. Queued mode also starts
without a model and accepts durable pending jobs for a separate dispatcher. A
one-cycle local fake runner currently exercises authorization and contact only.

Users own uploads. Job and output ownership is derived through the associated upload. Alembic manages database schema changes.

## Setup

Development currently takes place in Ubuntu through WSL2.

### Prerequisites

- Python 3.12+
- Git
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

Then select disabled or queued processing mode as described below. The inference extra
contains the model package and its GPU inference dependencies. These CPU locks
do not cover the GPU environment. See [Python Runtime](docs/python-runtime.md)
for the installation flags, target environment, and lock-update workflow.

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

### Configure Upload Limits

Uploaded files are limited to 200 MiB by default. Set a different positive byte
limit in the shell used to start the application:

```bash
export ISOJAM_MAX_UPLOAD_BYTES=104857600
export ISOJAM_MAX_AUDIO_DURATION_SECONDS=300
```

This example sets a 100 MiB file limit and a 5-minute duration limit. The default
duration limit is 10 minutes. Both settings must be positive integers; invalid
configuration fails startup before the model loads.

Uploads must contain readable, nonempty mono or stereo WAV audio at 8-96 kHz.
PCM and floating-point WAV audio are supported, including WAVEX. All samples are
checked for non-finite values through bounded block decoding. Invalid audio
returns `422`; files exceeding the byte or duration limit return `413`. Rejected
files are removed without saving upload metadata.

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

Health, registration, login, uploads, job status and existing output downloads
remain available. New processing jobs return `503` without creating a job record
or scheduling work. This mode does not perform CPU inference or submit jobs to an
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

Queued jobs remain pending until a dispatcher reserves and submits them. The
current local fake runner exercises coordination only: it authorizes one invocation
and reports contact, leaving the job processing without generating stems.
Pending/processing jobs count toward the allowance and cannot serve outputs (409).
Queued mode remains a development exercise until inference/publication is connected.

### Local dispatcher coordination exercise

After applying migrations, a dispatcher can run separately from the API. From
`backend`, with the same absolute `ISOJAM_DATABASE_PATH` as the queued API:

```bash
python -m app.dispatcher --once --adapter local-fake
```

This explicitly runs one cycle: commit reservation, commit submission intent,
then start `python -m app.fake_worker` as a separate child. The fake worker reads
its validated invocation from standard input, obtains permission and records one
heartbeat. Neither command starts FastAPI or loads the inference model. The local
adapter uses the shared SQLite file on the same host; it is not remote GPU control.

The dispatcher prints one JSON report. `worker_contact_recorded` means coordination
succeeded, not that separation completed. `idle` means no eligible work or occupied
capacity. Successful fake contact leaves the job processing and its attempt running;
subsequent cycles stay idle because result publication is not implemented yet.
Use disposable data for this intermediate exercise. For a fully isolated container
exercise that creates and removes its own volume, see [Containers](docs/containers.md).

Launch/report errors print `submission_unresolved` and preserve existing attempt
state. There is no automatic resubmission, timeout-based release, terminal transition,
expired-reservation scanner or polling loop yet. CLI defaults are 60 seconds for
pre-submission ownership, 300 for receiving execution permission, 30 for the local
fake-child wait, and 1000 milliseconds for each SQLite lock wait. The child timeout
is specific to this fake transport; it is not a GPU execution-time limit.

## Running

From `backend`, with the virtual environment activated and the signing secret configured:

```bash
uvicorn app.main:app --reload
```

Interactive API documentation is available at:

[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

Apply pending migrations with `alembic upgrade head` before starting an updated application.

## Running the CPU API in Docker

The API image uses the locked Python dependencies and disabled processing mode.
It accepts authentication and uploads; new processing jobs return `503` until a
worker is connected. Secrets are supplied at runtime, and a named volume keeps
SQLite and audio across container replacement.

See [Container Guide](docs/containers.md) for Docker setup, building the image,
configuration, migrations, startup, replacement, and the repeatable smoke check.

## Usage

```text
Register → Log in → Upload WAV → Create job → Poll status → Download stems
```

### 1. Register and Log In

Create an account with `POST /register`, then authenticate with `POST /login`. Both endpoints accept JSON:

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

### 2. Upload a WAV File

Send `POST /uploads` as multipart form data, using `audio_file` as the file field.

The response contains the upload ID.

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
Recovery is part of the planned worker system. Downgrading the receipt migration
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

### 4. Check Processing Status

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

### 5. Download a Stem

Request `GET /jobs/{job_id}/outputs/{stem}` with the same bearer authentication.

Downloads are available when the job is completed. Missing resources and resources owned by another user return 404. Missing or invalid authentication returns 401.

## Testing

Tests use temporary databases and storage, fake model sessions, and test signing secrets.

From `backend`, use a fresh Python 3.12 / Linux x86_64 environment as described
in [Python Runtime](docs/python-runtime.md), then install the test dependencies:

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

- [Container Guide](docs/containers.md)
- [Python Runtime](docs/python-runtime.md)
- [Project Scope](docs/project-scope.md)
- [Model Compatibility Spike](docs/model-spike.md)