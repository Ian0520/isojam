# CPU API container

This image packages the API for Linux x86_64 using Python 3.12.14. It runs in
`disabled` processing mode by default and contains no inference or test dependencies.
Authentication, uploads, job status, and existing output downloads remain
available; new jobs return 503. GPU workers and external job dispatch are later
milestones. Setting `ISOJAM_PROCESSING_MODE=queued` accepts durable pending jobs
without loading a model; they remain pending until the dispatcher is implemented.

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

## References

- [Docker Desktop installation](https://docs.docker.com/desktop/setup/install/windows-install/)
- [Docker Desktop WSL integration](https://docs.docker.com/desktop/features/wsl/)
- [Docker build practices](https://docs.docker.com/build/building/best-practices/)
- [Named volumes](https://docs.docker.com/engine/storage/volumes/)
- [Dockerfile reference](https://docs.docker.com/reference/dockerfile/)
