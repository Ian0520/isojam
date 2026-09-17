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

The application loads one model session during startup and reuses it across processing jobs.

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

Install the backend dependencies:

```bash
python -m pip install \
  fastapi uvicorn python-multipart \
  "sqlalchemy>=2.0,<3.0" alembic \
  "pwdlib[argon2]" email-validator pyjwt \
  "bs-roformer-infer @ git+https://github.com/openmirlab/bs-roformer-infer.git@de35ada5817b878da0194ee2860253dda3a9c2b2"
```

Dependencies are declared in `backend/pyproject.toml`. Direct installation is currently used while editable package configuration is being completed.

The inference dependency is pinned to an upstream commit that provides the programmatic `BSRoformerSession` API.

### Initialize the Database

From the `backend` directory:

```bash
alembic upgrade head
```

Metadata is stored in the project-level `data/isojam.db`. Uploaded and generated audio is stored under `data/`.

The ownership migration assumes there are no existing uploads without owners. Migrating an older database containing such uploads requires a separate data-migration plan.

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

From `backend`, install the test dependencies if needed:

```bash
python -m pip install pytest httpx2
```

Run the full suite:

```bash
python -m pytest
```

Coverage includes registration, login, token validation, ownership enforcement, migrations, persistence, storage, processing orchestration, model integration boundaries, and output downloads.

## Further Documentation

- [Project Scope](docs/project-scope.md)
- [Model Compatibility Spike](docs/model-spike.md)