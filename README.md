# IsoJam

IsoJam turns songs into practice tracks. Musicians can upload a song, separate it into instrument stems, and download isolated tracks or backing tracks for practice.

This is a backend-focused side project built with FastAPI, SQLAlchemy, and BS-RoFormer.

## Features

- Register and log in with email and password
- Authenticate requests using short-lived JWT access tokens
- Upload WAV audio files
- Create background source-separation jobs
- Retrieve processing status and download generated stems
- Persist users, uploads, jobs, and output metadata in SQLite
- Restrict uploads, jobs, and downloads to their owners

The current model produces vocals, drums, bass, guitar, piano, other, and instrumental outputs.

## Current Limitations

- Source separation supports WAV input only
- Audio files are stored on the local filesystem
- Metadata is stored in SQLite
- Processing uses in-process FastAPI background tasks
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
without importing the inference package or loading a model.

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

Then select disabled processing mode as described below. The inference extra
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

`ISOJAM_PROCESSING_MODE` accepts `local` (the default) or `disabled`. Invalid values
fail startup before model loading.

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

## Running

From `backend`, with the virtual environment activated and the signing secret configured:

```bash
uvicorn app.main:app --reload
```

Interactive API documentation is available at:

[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

Apply pending migrations with `alembic upgrade head` before starting an updated application.

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

- [Python Runtime](docs/python-runtime.md)
- [Project Scope](docs/project-scope.md)
- [Model Compatibility Spike](docs/model-spike.md)