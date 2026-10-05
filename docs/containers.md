# CPU API container

This image packages the API for Linux x86_64 using Python 3.12.14. It runs in
`disabled` processing mode by default and contains no inference or test dependencies.
Authentication, uploads, job status, and existing output downloads remain
available; new jobs return 503. GPU workers and external job dispatch are later
milestones. Setting `ISOJAM_PROCESSING_MODE=queued` accepts durable pending jobs
without loading a model. A one-cycle local dispatcher/fake-worker now exercises
permission/contact, durable fake WAV bundles and atomic database publication.
Real queued inference and remote control/storage remain later work.

The repository contains the recipe and application files. `docker build` creates
an image in Docker's storage; `docker run` creates a container from that image and
starts its saved Uvicorn command. A running container is the API server.

## Prerequisite: a running Docker engine

On Windows/WSL, install Docker Desktop for Windows x86_64 using its WSL2 backend.
Per-user installation is sufficient for this workflow. Start Docker Desktop,
complete its first-start prompts, and enable Ubuntu under Settings -> Resources
-> WSL Integration. Use Linux container mode.

Run these commands in the Ubuntu terminal:

```bash
docker version
docker info --format '{{.OSType}}'
```

The first command must report both Client and Server; the second must print
`linux`. A client executable alone cannot build or run containers.

The commands below start from the repository root, not `backend`:

```bash
cd /home/ian/projects/isojam
```

On another machine, use that machine's checkout location.

## 1. Build the image

```bash
docker build --platform linux/amd64 -t isojam-api:local ./backend
```

`./backend` is the build context: files available to the builder. The Dockerfile
starts from an official Python/Debian image pinned by digest. It installs the
hashed API dependency lock, builds IsoJam using the installed build requirements,
and includes Alembic and its migrations.

`.dockerignore` permits only required build files, application source, and
migrations. Local secrets, virtual environments, tests, audio, databases, and
packaging output are excluded. Copying the lock before application source lets
Docker reuse the dependency installation layer when only source changes.

The recipe uses one stage for this first API image. It retains the locked Python
build tooling, but installs no compiler, development extra, or model package.
This favors a short, inspectable recipe. A later size optimization can separate
build tooling into a builder stage if measurements justify it.

## 2. Prepare runtime settings

```bash
cp backend/.env.container.example backend/.env.container
docker run --rm isojam-api:local python -c 'import secrets; print(secrets.token_hex(32))'
```

Put the generated value after `ISOJAM_JWT_SECRET_KEY=` in
`backend/.env.container`. Keep the secret stable across container replacements.
The real settings file is ignored by Git and excluded from the build context.
Docker reads it at launch through `--env-file`; it is not copied into the image.

The template uses paths **inside the container**:

```text
ISOJAM_PROCESSING_MODE=disabled
ISOJAM_DATABASE_PATH=/var/lib/isojam/metadata/isojam.db
ISOJAM_AUDIO_STORAGE_DIR=/var/lib/isojam/audio
```

Leave `ALEMBIC_DATABASE_URL` unset so migrations and the API select the same
metadata file. Optional request/upload/job limits use the existing environment
settings documented in the README.

## 3. Create persistent storage and migrate

```bash
docker volume create isojam-data
docker run --rm \
  --env-file backend/.env.container \
  --mount type=volume,src=isojam-data,dst=/var/lib/isojam \
  isojam-api:local python -m alembic upgrade head
```

A named volume is managed by Docker and survives removal of containers that use
it. Both migrations and the API must attach the same volume at the same path.
The mount supplies SQLite metadata and the audio directories.

The image runs as UID/GID 10001. Its storage directory is created with that
ownership during the build. Docker populates a new empty named volume from that
image directory, preserving the prepared permissions. An existing volume retains
its own contents and permissions. A host-directory bind mount requires separate
permission setup; these instructions use a named volume.

Arguments after the image name replace the default Uvicorn command, so this
short-lived container runs Alembic and exits. `--rm` removes that container;
it does not remove the explicitly named `isojam-data` volume.

Migrations are deliberate, separate operations. Server startup does not run them
implicitly. Creating a fresh volume does not import an existing development
database or audio collection; moving existing data requires its own migration.

## 4. Start the API

```bash
docker run --detach --name isojam-api \
  --env-file backend/.env.container \
  --mount type=volume,src=isojam-data,dst=/var/lib/isojam \
  --publish 127.0.0.1:8000:8000 \
  isojam-api:local
```

This starts the image's saved command:

```text
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Uvicorn listens on the container's network interface. The publication maps host
port 8000 to container port 8000 and binds it to host loopback for this local
workflow. `EXPOSE 8000` in the Dockerfile documents the port; the run command
actually publishes it.

Visit http://127.0.0.1:8000/health or http://127.0.0.1:8000/docs.

```bash
docker logs isojam-api
docker stop isojam-api
docker start isojam-api
```

The JSON-array CMD starts Python directly so Docker's stop signal reaches
Uvicorn for shutdown. `docker start` reuses the existing container and its saved
configuration. To use a newly built image or changed environment settings,
replace the container instead.

## 5. Replace the application while keeping data

For this local, single-API workflow:

1. Stop and remove the API container with `docker stop isojam-api` followed by
   `docker rm isojam-api`.
2. Build the updated image using step 1.
3. Apply pending migrations using step 3 with the same settings and volume.
4. Create the API container again using step 4 with the same settings and volume.

Stopping the API avoids serving requests while applying schema changes. Removing
the container removes its disposable filesystem layer, while the named volume
retains accounts, metadata, and audio. This is persistence on one Docker host;
it is not a backup or automatic storage replication to another machine.

## Repeatable container smoke check

After building the image, run from the repository root:

```bash
python3 backend/scripts/smoke_container.py --image isojam-api:local
python3 backend/scripts/smoke_container.py --image isojam-api:local --processing-mode queued
python3 backend/scripts/smoke_container.py --image isojam-api:local --processing-mode queued --dispatch-fake-worker
```

This host script uses only Python's standard library and Docker CLI. It generates
its own temporary secret and uniquely named, labeled volume; it does not read the
real `.env.container` or use `isojam-data`. It checks:

- Non-root execution, expected configured paths, matching installed dependency
  versions, absence of inference/test packages, and writable mounted storage.
- Alembic migrations using the same image and volume as the API.
- Real HTTP health, registration, login, WAV upload, and missing-authentication
  401 for job submission. Disabled mode checks authenticated 503; queued mode
  checks pending acceptance, keyed replay, status, and unavailable outputs (409).
- Clean shutdown followed by removal and recreation of the API container.
- Login after replacement and unchanged saved WAV bytes in the volume. Disabled
  mode leaves zero jobs; queued mode preserves one pending queued job and its
  receipt, without creating an attempt/output or another job on replay.

With `--dispatch-fake-worker` (queued mode only), the script additionally runs a
separate one-cycle dispatcher container after the API is stopped. The same CPU
image uses a different command to launch its local fake-worker child. It verifies
one succeeded attempt with invocation/start/heartbeat/finish evidence, a completed
job, seven published output rows, and verified fake WAVs plus their selected
manifest in the volume. It checks an idle second dispatcher container, restarts
the API, replays the original submission key, and downloads all seven owned WAVs
with exact published hashes. This demonstrates process roles, publication and
the shared same-host volume. The files are dummy audio; the model is not loaded.

The script publishes a dynamically allocated port on host loopback and removes
only resources carrying its run-specific label, including on failure. The built
image remains available. This checks container packaging and API persistence;
it does not validate model execution or remote hosting.

## Updating the image deliberately

Application changes require a new build; dependency changes require regenerating
and validating the lock first. The base-image digest also requires deliberate
updates to receive Python/system-library fixes. A tag such as `3.12-slim` can move,
while the pinned digest identifies a specific base image. After a base or package
update, rerun the smoke check and appropriate application tests.

## Validation status

Validated on 2026-10-01 using Docker Desktop's WSL2 Linux engine (Docker 29.8.1).
The digest-pinned image built successfully for linux/amd64 with Python 3.12.14.
The container smoke passed: UID/GID 10001, exact dependency versions, no
inference/test packages, writable volume storage, migrations, real HTTP
registration/login/upload, disabled and unauthenticated job rejection, clean
shutdown, and account/upload persistence after container removal and recreation.
The final check compared saved WAV bytes by SHA-256 and confirmed zero job rows.
The smoke script removed all of its labeled containers and volume.

All 266 backend tests passed in a separate disposable container based on this
image. Locked development dependencies were installed in that temporary
container, tests/scripts were mounted read-only, and test execution ran as UID/GID
10001. Dependency consistency, Ruff lint, and formatting also passed there.
Those development dependencies and mounted test files are not in the runtime
image. Host lint/formatting checks passed as well.

Docker reports approximately 366.3 MiB of image size for this build; this is image
storage size, not application RAM usage or registry download size. The image is
available locally as `isojam-api:local`; validation left no API server running.
These checks do not yet validate remote hosting or GPU processing.

### Queued-mode validation (2026-10-05)

The updated image `isojam-api:queued-check` passed the smoke script in both
CPU modes. Queued mode preserved one pending job and its receipt through
replacement, returned the same job on keyed replay, kept outputs unavailable,
and preserved exact uploaded WAV bytes. No attempt or output was created.
Disabled mode retained 503 and zero job/receipt behavior. All resources created
by these smoke runs were removed; existing development/exercise data was unused.

All 742 backend tests passed on the host and in the locked container as UID 10001.
Host Ruff lint/format passed across 70 maintained Python files. The separate
runner and GPU execution remain unimplemented.

### Dispatcher-command validation (2026-10-05)

The updated `isojam-api:dispatcher-check` image passed the queued smoke with
`--dispatch-fake-worker`: real HTTP acceptance and replacement preceded a separate
dispatcher container and fake-worker child, committed contact/processing state,
and an idle second dispatcher container. No stems were generated or published.
All resources created by the smoke were removed; existing data was unused.

All 790 tests passed on the host and in the locked container as UID 10001.
Lint/format passed across 75 maintained Python files. These checks validate the
local coordination commands, not hosted GPU execution or complete recovery.

## References

- [Docker Desktop installation](https://docs.docker.com/desktop/setup/install/windows-install/)
- [Docker Desktop WSL integration](https://docs.docker.com/desktop/features/wsl/)
- [Docker build practices](https://docs.docker.com/build/building/best-practices/)
- [Named volumes](https://docs.docker.com/engine/storage/volumes/)
- [Dockerfile reference](https://docs.docker.com/reference/dockerfile/)


### Durable fake result bundle verification (2026-10-05)

The `isojam-api:results-check` image passed the queued smoke with
`--dispatch-fake-worker`. The worker saved seven tiny fake WAVs and a manifest in
the disposable volume. A fresh container independently verified identity, profile,
stem set, canonical paths, sizes, SHA-256 and WAV contents. The job stayed processing,
the attempt stayed running, no output rows were published, and the second dispatch
cycle stayed idle. The script cleaned up its labeled containers and volume; the
existing Docker exercise volume was preserved.


### Atomic publication and owned download verification (2026-10-05)

The `isojam-api:publication-check` image passed the queued smoke with
`--dispatch-fake-worker`. After the separate worker exited, the dispatcher
published all seven output rows with the selected manifest and job/attempt
completion. A fresh container verified the stored paths and manifest hash; an
idle second cycle created no additional work. The script then restarted the API,
logged in, downloaded every owned dummy WAV with its exact published SHA-256,
and replayed the original submission key to retrieve the completed job.
The full locked suite passed 918 tests as UID 10001. Verification resources were
removed using their run-specific labels. For an existing database, stop control
processes and apply `python -m alembic upgrade head` before using this dispatcher.


### Durable controller exit evidence verification (2026-10-05)

The isojam-api:stop-evidence-check image passed the isolated queued smoke with
--dispatch-fake-worker. A fresh container verified the authorized local launch
UUID/PID, actual exit code zero, and start/heartbeat/stop/finish chronology alongside
the selected manifest and all seven outputs. The restarted API served every owned
WAV with its exact published SHA-256; a second dispatch cycle remained idle.

All 1,013 tests passed on the host and in a separate disposable test container
with locked CPU dependencies as UID 10001. Lint/format passed across 84 maintained
Python files. Verification containers/volumes were removed, while the existing
isojam-exercise-data volume remained present. This verifies local fake execution,
not remote GPU termination or automatic restart reconciliation. For existing data,
stop API/control/worker processes and apply python -m alembic upgrade head before
using the new code; migration does not invent exit evidence for historical rows.


### Exercise interrupted publication recovery

Build the updated image and run the isolated restart smoke from the repository root:

```bash
docker build --platform linux/amd64 -t isojam-api:recovery-check ./backend
python backend/scripts/smoke_container.py --image isojam-api:recovery-check --processing-mode queued --dispatch-fake-worker --recover-publication
```

The smoke deliberately interrupts the publication commit after a real child exits.
The first disposable dispatcher container leaves a processing job, running attempt,
complete saved bundle and controller-confirmed stop proof, with zero output rows.
A fresh container runs --reconcile-only and publishes that same invocation. It
checks unchanged launch UUID/PID and stop timestamp, one attempt, all seven outputs,
and exact downloaded WAV hashes after API restart. A repeated cycle stays idle.
The run-specific labeled containers and volume are removed afterwards.

For existing queued data, the ordinary --once dispatcher first attempts one such
recovery. To restrict an operator action to recovery without dispatching new work:

```bash
python -m app.dispatcher --once --adapter local-fake --reconcile-only
```

Use the same database/audio volume and apply the Stage 4A migration before running
this code. Unknown termination or invalid files retain the occupied attempt.
This same-host fake exercise does not establish remote GPU cancellation/recovery.


Stage 4B verification passed on isojam-api:recovery-check: 1,061 host tests,
1,061 locked-container tests as UID 10001, lint/format across 85 maintained Python
files, and the isolated interrupted-publication smoke above. The fresh recovery
container preserved the launch UUID/PID and stop timestamp, published one original
attempt, and the restarted API served seven exact-hash outputs. Verification
containers/volumes were removed; isojam-exercise-data remained present.
