# Local API and GPU processor with Docker Compose

For complete first-time instructions and account creation, start with the
[README quickstart](../README.md#start-locally-with-docker-compose). This guide
covers the stack's operation, storage, shutdown and dependency maintenance.
For startup failures, see [operations and troubleshooting](operations.md).

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

Follow the [README setup steps](../README.md#start-locally-with-docker-compose)
to check Docker, create `.env` without replacing an existing secret, build both
images, prepare models and start the stack. Compose needs no native Python
virtual environment. Keep `.env` stable across container replacements.

The first build downloads large CUDA wheels. The model setup command downloads
the pinned checkpoint, copies the configuration bundled in the pinned inference
package and verifies their sizes and SHA-256 hashes. The checkpoint uses the
registry digest; the cached YAML must match the bundled config, whose bytes differ
from the upstream remote-download YAML. Repeating it reuses valid cached files.
Model weights are not embedded in the image or committed to Git. Normal processor startup only verifies the cache;
it fails with a useful error if files are missing or damaged.

Migrations must succeed before the processor starts. The processor must pass its
GPU/cache preflight before the API starts. Open <http://127.0.0.1:8000/> and upload
a short WAV after creating an account to try real processing. The terminal shows
both services' logs. The example `.env` opens registration; the application
otherwise defaults to `closed`. See
[registration configuration](configuration.md#configure-registration).
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

After source changes, drain the services, then rebuild and replace them:

```bash
docker compose stop
docker compose up --build --abort-on-container-failure
```

Stopping first also ensures a future migration cannot run while an old API or
worker still accesses the database.

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

## Verification recorded on 2026-10-18

Both images built from the pinned Python base and hash-locked dependencies and
passed `pip check`. The GPU image passed its CUDA/cache preflight on an NVIDIA
GeForce RTX 4060 Laptop GPU with PyTorch 2.13.0+cu130. Missing GPU access and model
assets fail explicitly; model preparation reused the existing read-only cache.
Both GPU dependency locks regenerated identically with the pinned tooling.

The real model service smoke also passed inside the GPU image: API restart
preserved one invocation and seven owned downloads, an interrupted worker gained
confirmed stop proof without automatic inference retry, and the same dispatcher
completed later work. It finished with exactly three jobs and three attempts.

Both the CPU fake-worker and real GPU Compose checks passed. During real
inference, the API stopped before the processor; the processor published its
current result and exited zero while a second job stayed pending without an
attempt. Recreated containers retained login, the original attempt, unchanged
upload bytes and all seven exact downloads, then processed the queued job.
Each smoke removed its disposable data and Docker resources. These short
synthetic WAV checks establish lifecycle behavior, not throughput or audio quality.

The complete CPU suite passed all 1,378 tests, including 26 processor tests, in
378.97 seconds under locked dependencies as UID/GID 10001. Ruff lint and formatting
checks passed across all 109 maintained Python files. The run reported fifteen
existing SQLAlchemy `Result.tuples()` deprecation warnings in job-history tests.
