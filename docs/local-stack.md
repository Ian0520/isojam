# Local API and GPU processor with Docker Compose

Run these commands in Ubuntu/WSL2 from the repository root. This stack is for
one local NVIDIA GPU, one processor and local browser access. It uses its own
Docker volumes; it does not import the database or audio from your native Python
environment.

## What runs where

| Service | Image and responsibility |
| --- | --- |
| `migrate` | CPU API image; applies Alembic migrations and exits before services start. |
| `api` | CPU API image; browser, authentication, uploads, queued job submission and downloads. No Torch or model. |
| `processor` | GPU image; checks CUDA and cached model files, then runs the continuous dispatcher and its model worker children. |
| `models` | Explicit setup command in the GPU image; downloads and verifies model assets. It is not a background service. |

The API and processor share the `data` volume at `/var/lib/isojam`, containing
SQLite metadata and audio. The processor reads model weights from the separate
`models` volume. Database identifiers and file paths therefore refer to the same
data in both containers. This is a single-host design, not a remote worker setup.

The dispatcher and its worker stay together in the processor container. Their
process IDs and process groups must be visible to the same controller so it can
confirm execution stopped before publishing a result. Only the API publishes a
network port, bound to `127.0.0.1`.

## Host requirements

- Linux Docker engine and Docker Compose supporting `gpus: all` and Compose
  override tag `!reset` (the latter is used by the CPU smoke only).
- On Windows, Docker Desktop with its WSL2 backend and Ubuntu integration.
- A compatible NVIDIA host driver and Docker GPU access. The GPU lock targets
  Linux x86_64, Python 3.12 and PyTorch's CUDA 13.0 build.
- Enough disk space for CUDA/PyTorch images, build cache, model weights and WAV
  outputs. These are substantially larger than the CPU API image. On WSL, also
  check Windows host free space (`df -h /mnt/c`), not only Linux virtual capacity
  (`df -h /`). First-time lock generation can cache many additional release
  wheels beyond those installed in the image.

The host supplies the GPU driver. The GPU image supplies Python, Torch, CUDA user
libraries and the pinned inference package. A container cannot make an unsupported
host GPU or driver compatible. Build with `--platform linux/amd64` when building
the images manually on another architecture; that does not provide a CUDA GPU.

## First setup

Create a private configuration file and generate a signing secret:

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Paste the generated value after `ISOJAM_JWT_SECRET_KEY=` in `.env`. Keep the file
and secret stable across container replacements. Git ignores `.env`. The example
explicitly opens registration for local account creation; switch to `closed` or
`invite` later as described in [registration configuration](configuration.md#configure-registration).

Build both images, then prepare the persistent model cache:

```bash
docker compose build
docker compose run --rm models
```

The first build downloads large CUDA wheels. The model setup command downloads
the pinned checkpoint and configuration and verifies their sizes and SHA-256
hashes. Repeating it reuses valid cached files. Model weights are not embedded in
the image or committed to Git. Normal processor startup only verifies the cache;
it fails with a useful error if files are missing or damaged.

Start the stack:

```bash
docker compose up --abort-on-container-failure
```

Migrations must succeed before the processor starts. The processor must pass its
GPU/cache preflight before the API starts. Open <http://127.0.0.1:8000/> and upload
a short WAV to try real processing. The terminal shows both services' logs.
The model is loaded by the worker for each job, rather than by the API.

The `models` setup service stays outside normal `up` because it has a setup
profile. Migrations are an explicit service; application startup itself does not
silently create or alter database tables.

## Daily startup and shutdown

```bash
# Start with the built images and watch logs.
docker compose up --abort-on-container-failure

# Or start in the background and wait for health checks.
docker compose up -d --wait
docker compose logs --follow api processor

# From another terminal, stop while preserving data and containers.
docker compose stop

# Or stop and remove containers/network while preserving the named volumes.
docker compose down
```

Ctrl+C in the foreground `up` terminal also stops the stack. The API stops first,
closing admission of new requests; the processor finishes its current dispatch
cycle and exits without taking the next job. Its stop grace is 35 minutes to
cover the default 30-minute worker deadline and result publication. A queued job
that has not started stays pending for the next startup. Uvicorn can report exit
code 143 after its normal SIGTERM shutdown; the processor should exit zero.

Let shutdown finish. A second Ctrl+C, a short explicit stop timeout or a forced
kill can interrupt inference and leave an attempt requiring inspection. An old
heartbeat is not evidence that GPU execution ended, so startup does not blindly
retry uncertain work. See [worker recovery](model-worker.md).

After source changes, rebuild and replace the services:

```bash
docker compose up --build --abort-on-container-failure
```

This retains the data/model volumes. Restarting an existing container alone does
not copy in changed source or settings. A changed Compose environment also needs
container recreation. Use `docker compose ps -a` and `docker compose logs` to
inspect startup failures. Containers have no automatic restart policy so a failed
processor remains visible instead of restarting in a loop.

`docker compose down --volumes` deletes this stack's database, recordings, outputs
and cached models. It is not the normal shutdown command. Avoid changing the
Compose project name unless you intend to use a separate set of volumes.

Docker stdout/stderr logs rotate at three 10 MB files per service. Invocation
logs and saved audio inside `data` do not yet have automatic retention; neither
volume has a backup policy. This stack does not expose the application publicly.

## GPU image and dependency maintenance

`backend/Dockerfile.gpu` uses the same pinned Python base and API dependency lock
as the CPU image, then installs `requirements-gpu.txt`. Its input file constrains
shared packages to the API versions and pins Torch and the inference source
commit. The generated GPU lock records transitive versions and artifact hashes.
`requirements-gpu-build.in` and its generated lock separately pin the upstream
Hatchling build backend, installed before reading the inference archive metadata.
The image runs as UID/GID 10001, matching the API's data ownership.

In the separate Linux/Python 3.12 lock-tool environment described in
[Python runtime](python-runtime.md), regenerate with:

```bash
cd backend
python scripts/compile_requirements.py --gpu
```

Use `--gpu --upgrade` for a deliberate GPU dependency update. Building from a
hash-checked source archive keeps the upstream commit fixed; its build uses the
already locked build tools without a second isolated dependency resolution.
Review dependency changes and repeat real GPU checks after an update.

The preflight imports Torch once, verifies the expected CUDA build, GPU access
and both model artifacts, then uses `exec` to become the dispatcher. This releases
the preflight's CUDA state and preserves signal delivery. A lightweight health
check checks the readiness marker and dispatcher process without importing Torch
or allocating GPU memory on every health probe. It establishes startup/liveness,
not successful inference or queue throughput.

## Disposable verification

The CPU check exercises Compose ordering, real API requests, a clearly substituted
fake worker, graceful stop, volume persistence and container replacement:

```bash
python3 backend/scripts/smoke_compose.py --api-image isojam-api:local-stack
```

To exercise the real GPU image instead, supply an existing model-cache directory
containing the pinned model's subdirectory. It is mounted read-only and is never
downloaded into or modified by this smoke:

```bash
python3 backend/scripts/smoke_compose.py \
  --api-image isojam-api:local-stack \
  --processor-image isojam-processor:local-stack \
  --models-dir "$HOME/.cache/bs-roformer-infer"
```

The real check submits original synthetic audio, requests Compose shutdown during
inference with another job queued, and verifies the active result finishes with
saved exit proof while the next job stays pending. Recreated containers retain
login, the original attempt, input bytes and seven exact downloads, then process
the remaining job. Both modes use a unique Compose project, a generated test
secret and fresh volumes, and remove only those resources afterward. They do not
use `.env`, personal recordings or the normal stack's data.

The existing [real model service smoke](model-worker.md#real-service-verification-on-2026-10-17)
can also run inside the GPU image to check interrupted-worker handling. CPU CI cannot
prove CUDA compatibility; actual GPU checks remain separate.
