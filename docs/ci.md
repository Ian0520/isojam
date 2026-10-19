# Continuous integration

Every push and pull request runs `.github/workflows/backend.yml`. A manually
triggered run is also available in GitHub Actions. The workflow has read-only
repository permissions and needs no application secrets or GPU.

The pipeline checks browser JavaScript modules with Node 24 and runs the
authentication, upload and history tests using Node's built-in test runner. Upload
checks cover supported formats, canonical multipart types, preserved filenames/bytes,
empty/oversized files and validation before submission. Authentication
checks cover invitation submission, login-only requests, generic rejection,
incomplete responses and recovery when registration succeeds but automatic login
fails. History checks cover cursor
continuation, refresh, failed-page retry, repeated load-more clicks, and delayed
responses after account reset or refresh. No frontend dependencies or build step
are needed. The pipeline then builds the real Linux x86_64 CPU image, installs
hash-locked test tools in a disposable
container, checks lint/formatting, and runs pytest as the
runtime UID/GID 10001. Source is mounted read-only. Tests use temporary metadata
and audio storage; the actual project database and container settings are unused.
The two original generated migrations are excluded from formatting/lint checks;
all maintained application, test, script and migration files are checked.
The checker requires FFmpeg and ffprobe in the API image so real format conversion
tests cannot silently skip in CI. Native test runs without those tools may skip
the real-codec cases.

The runtime image never gains test tools. They are installed in a separate
container that is removed after it exits. Python, operating-system image and
runtime dependencies are the same as deployment. The checkout and Node setup
actions are pinned to commits, and overlapping runs for the same branch are cancelled.

Two real HTTP container smokes then exercise disabled mode and queued fake
execution with deliberately interrupted publication. They verify migrations,
packaged browser assets and runtime settings, authentication, ownership, exact
WAV hashes and data persistence across API container replacement. Fresh recovery publishes the original stopped attempt
without another worker launch. Smoke resources have unique names/ownership labels
and are removed afterwards. Real GPU inference is validated separately locally.

The packaged owner command issues an invitation on the disposable volume before
the API starts. The first server runs invitation-only registration: invalid codes
are rejected, and the correct code creates its account once. Replacement in the
same mode preserves the used flag and rejects replay. Another replacement closes
registration and preserves login and existing uploads/jobs. The invitation hash,
seven-day expiry and use timestamp survive all replacements. The runtime default
is also checked as closed. The raw code is kept out of smoke output.

A separate Compose smoke runs the API and processor in different containers with
an explicit CPU fake-worker override. It checks migration ordering, API admission,
MP3 upload/conversion, graceful shutdown, original-attempt persistence and exact downloads after both
containers are recreated. Each run owns and removes its own Compose project and
volumes. The real GPU version is described in [local stack](local-stack.md).

## Run the same checks locally

From the repository root with Docker running:

```bash
# Browser syntax and controller tests (Node 24); also run in CI.
node --check backend/app/static/app.js
node --check backend/app/static/auth.js
node --check backend/app/static/history.js
node --check backend/app/static/uploads.js
node --check backend/app/static/imports.js
node --test backend/tests/test_*_browser.mjs
docker build --platform linux/amd64 -t isojam-api:ci backend
docker build --platform linux/amd64 -f backend/Dockerfile.youtube -t isojam-youtube:ci backend
python3 backend/scripts/check_container.py --image isojam-api:ci
python3 backend/scripts/smoke_container.py --image isojam-api:ci
python3 backend/scripts/smoke_container.py --image isojam-api:ci --processing-mode queued --dispatch-fake-worker --recover-publication
python3 backend/scripts/smoke_compose.py --api-image isojam-api:ci --import-image isojam-youtube:ci --audio-format mp3
```

A failed check exits nonzero and fails the GitHub job. Inspect the first failing
step and reproduce its command locally. Container checks print the actual test
UID/GID. The CPU suite does not prove GPU or hosted-provider compatibility.

Browser checks cover import retries, account isolation, reload recovery and the
handoff to separation. Page tests execute the actual module against the HTML's
element IDs with controlled HTTP responses. Real-browser layout checks and the
[opt-in live YouTube check](youtube-imports.md#dependency-maintenance-and-checks)
remain separate from deterministic CI tests.

## Delivery boundary

This is CI: it verifies a proposed change automatically. It does not deploy a
server or publish an image. Release promotion, secrets, migrations, health checks
and rollback are separate deployment work. Require the Backend CI check in branch
protection if you want GitHub to prevent merging a failing pull request; the
workflow itself does not change repository settings.
