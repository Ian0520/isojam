# IsoJam

IsoJam is a music-practice web application that uses audio source separation to generate isolated instrument tracks and backing tracks from uploaded songs.

## Status

Early development.

The initial source-separation feasibility spike has been completed, with BS-RoFormer-SW selected as the current model for the first version.

The backend currently supports an end-to-end WAV processing flow:

- upload an audio file
- create a processing job
- run BS-RoFormer source separation in the background
- retrieve job status and generated output metadata
- download generated stems through the API

Current limitations:

- job and upload metadata are stored in memory and are lost when the server restarts
- source separation currently supports WAV input only
- generated audio is stored on the local filesystem
- processing currently runs as an in-process FastAPI background task rather than through a dedicated worker
- authentication and user accounts are not yet implemented

## How It Works

```text
WAV upload
    ↓
processing job
    ↓
BS-RoFormer source separation
    ↓
generated stems
    ↓
download through API
```

The current model produces the following outputs:

- vocals
- drums
- bass
- guitar
- piano
- other
- instrumental

## Setup

### Prerequisites

- Python 3.12+
- Git
- NVIDIA GPU with a compatible CUDA environment for GPU inference

IsoJam is currently developed and tested primarily in Linux/WSL2.

### Install

Clone the repository and enter the backend directory:

```bash
git clone https://github.com/Ian0520/isojam.git
cd isojam/backend
```

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install the backend and its dependencies:

```bash
python -m pip install -e .
```

The BS-RoFormer inference dependency is pinned to a specific upstream Git commit to use the programmatic `BSRoformerSession` API.

## Running

From the `backend` directory with the virtual environment activated:

```bash
uvicorn app.main:app --reload
```

The interactive API documentation is available at:

```text
http://127.0.0.1:8000/docs
```

The source-separation model is loaded when the application starts and reused across processing jobs.

## Usage

The current API flow is:

### 1. Upload a WAV file

```text
POST /uploads
```

The response contains an upload ID.

### 2. Create a processing job

```text
POST /jobs
```

Use the upload ID returned by the previous request.

### 3. Retrieve job status

```text
GET /jobs/{job_id}
```

A completed job includes output URLs for the generated stems.

Example:

```json
{
  "id": "job-id",
  "status": "completed",
  "upload_id": "upload-id",
  "outputs": {
    "guitar": "/jobs/job-id/outputs/guitar",
    "vocals": "/jobs/job-id/outputs/vocals"
  }
}
```

### 4. Download a generated stem

```text
GET /jobs/{job_id}/outputs/{stem}
```

For example:

```text
GET /jobs/{job_id}/outputs/guitar
```

## Current Architecture

```text
FastAPI API
    ↓
in-process background task
    ↓
processing layer
    ↓
source-separation adapter
    ↓
BSRoformerSession
    ↓
BS-RoFormer-SW
    ↓
local filesystem storage
```

The FastAPI application owns a single model session through its application lifespan. Processing jobs reuse that session instead of loading the model for every request.

Uploaded files and generated outputs are stored under the project-level `data/` directory.

## Testing

From the `backend` directory:

```bash
python -m pytest
```

The test suite covers the API, local storage, job lifecycle, processing orchestration, source-separation adapter, model-session integration boundaries, and output downloads.

## Documentation

- [Project Scope](docs/project-scope.md)
- [Model Compatibility Spike](docs/model-spike.md)