# Real model worker

## Current boundary

The queued path supports explicit `local-model` execution on the SQLite host.
`app.model_processing.process_model_job` produces verified files,
`app.model_worker` obtains execution permission and owns the model lifecycle, and
`LocalModelWorkerAdapter` launches and waits for that worker. The dispatcher then
publishes the verified bundle or finalizes a confirmed unsuccessful execution.
`local-fake` remains available for CPU-only exercises. Local API background
processing keeps its existing behavior. `--serve` polls continuously; `--once`
performs one explicit cycle.

For the initial same-host release, retain the SQLite queue and start with one
active inference subprocess. This reuses the existing reservation, execution and
publication rules. It also leaves their maintenance with this project. Adopting
Celery remains a later decision, rather than combining a queue migration with
model integration. A separate process per job allows confirmed exit before
publication, at the cost of loading the model for each job.

## Run one real queued job

Use the existing Python 3.12 inference environment on Linux/WSL2, CUDA and cached
BS-RoFormer-SW assets. Apply the existing Alembic migrations first; this slice
requires no new migration or dependency. The CPU API Docker image has no Torch
or model dependencies and cannot run this worker.

For this initial workflow, run both processes directly on the same Linux host.
In both shells, use matching absolute `ISOJAM_DATABASE_PATH` and
`ISOJAM_AUDIO_STORAGE_DIR`, plus matching upload/duration limits. From `backend`,
start the API with its signing secret configured:

```bash
export ISOJAM_PROCESSING_MODE=queued
export ISOJAM_REGISTRATION_MODE=open  # Local demo account creation only.
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Submit a WAV through the browser, then in the other activated inference shell:

```bash
python -m app.dispatcher --once --adapter local-model
```

One cycle handles one saved recovery or one new delivery. To discover later jobs
automatically, run the continuous form instead:

```bash
python -m app.dispatcher --serve --adapter local-model
```

Keep `local-fake` on disposable exercise data, rather than selecting
it for personal recordings. The chosen adapter supplies the trusted verification
profile; do not switch adapters/profiles while attempts remain unfinished.

Model wait defaults to 1,800 seconds, including startup, loading, inference and
cleanup; `--worker-timeout-seconds` permits 1-7,200. Fake wait remains 30 by default,
maximum 60. These limits differ from the 300-second execution-grant deadline:
the worker obtains its grant before loading the model. Expiry cannot cancel or
replace an already-authorized execution.

For recovery without launching any new work:

```bash
python -m app.dispatcher --once --adapter local-model --reconcile-only
```

This recovery command needs the ordinary CPU API dependencies and matching
configuration/storage, not a loaded model. It does not import Torch.

## Continuous operation and shutdown

The loop uses one dispatcher UUID for its process lifetime and calls the same
bounded `dispatch_once` operation. Completed, recovered and failed jobs permit
the next cycle immediately. Idle or unresolved cycles and SQLite contention wait
two seconds by default; `--poll-interval-seconds` accepts 0.1-60 seconds. No
database connection is held while waiting. Repeated identical reports are
suppressed, and each changed result is flushed as one JSON line. Unexpected
errors include a stderr traceback and exit nonzero for the supervisor to inspect.

SIGTERM/SIGINT set a stop flag. The active cycle finishes and publishes or records
its outcome, then the loop prints `stopped` and exits zero without taking another
job. An idle stop waits at most the polling interval. Both local adapters put
their child in a separate Linux session, so terminal Ctrl+C reaches the
controller's handler without also interrupting its active worker.

[isojam-dispatcher.service](../deploy/isojam-dispatcher.service) is a systemd example,
not an installed service. Configure its account, paths and environment for the
chosen host before enabling it. The example expects the inference environment at
`/opt/isojam/backend/.venv`, configuration in `/etc/isojam/isojam.env`, cached
weights accessible to the `isojam` account, and matching storage permissions for
the API and worker. It restarts unexpected exits after five seconds. `KillMode=mixed`
initially signals only the controller, while the 35-minute stop allowance covers
the default 30-minute worker limit plus cleanup/publication. Increase that
allowance if increasing the worker limit. Forceful termination can still leave
an invocation without durable exit proof; a restart does not invent it.

Before taking a new job, reservation can also recover the sole occupied attempt
when it is still `reserved`, owned, expired and attached to a pending job. This
uses the existing takeover transition under the same write lock: keep the attempt
ID/number, increase its generation and invalidate the previous token. No
inference had been submitted. Multiple occupied attempts, unknown owners and
attempts at `submitting` or later never qualify. This is pre-submission recovery,
not an inference retry. A stale controller cannot subsequently begin submission
with its old token.

## Worker lifetime and diagnostics

Every delivery gets a fresh launch UUID. The worker atomically obtains permission
using its attempt/invocation/generation, launch UUID and actual PID. A denial or
lost grant acknowledgement never reaches model loading. The initial heartbeat
must succeed, followed by contact every 15 seconds while loading, processing and
closing the model. Transient periodic contact errors are logged; heartbeats grant
no additional execution and their absence never releases capacity. Explicit
rejection prevents starting inference if seen after loading, and prevents a ready
report if seen after processing. Publication always rechecks database authority.

The controller gives each worker a new Linux session/process group. The pinned
eager CUDA path was inspected and runs inference in the direct worker process;
it does not use multiprocessing, distributed inference or `torch.compile`.
Normal exit requires the group to be gone before stop proof is saved. On timeout
the controller sends SIGKILL to the group and waits at most five seconds. Failure
to confirm termination, a surviving group member or failure to commit stop proof
keeps capacity occupied. An interrupted controller cleans up with a bounded wait
but does not invent durable proof. Arbitrary descendants escaping their session
are outside this trusted runner's contract; this is not a container/cgroup
isolation boundary. Revisit the lifetime contract when changing the inference
backend or adding subprocess-based preprocessing.

The control reply is one bounded JSON message. Before model loading, the worker
duplicates its original stdout descriptor for that reply, then redirects OS
stdout to stderr. Python prints and native library stdout writes therefore go
to `ISOJAM_AUDIO_STORAGE_DIR/worker-logs/<invocation>-<launch>.log`, not into JSON.
Files are created exclusively with mode 0600; inherited login signing secrets
are removed from the worker environment. Log retention and total disk quotas
remain deployment operations work.

## Completion versus confirmed failure

Verification still occurs outside database transactions. The trusted model
profile is passed through normal publication and restart reconciliation; manifests
never select their own policy. The shared report parser checks the actual child
PID, invocation IDs, exit/status pairing and reply size for both local adapters.

| Durable evidence | Outcome |
| --- | --- |
| Confirmed stop plus a complete valid bundle, including a nonzero exit after saving it | Publish completion; never repeat inference |
| Confirmed nonzero exit with no manifest entry | Atomically mark the current attempt and job failed, release capacity; no automatic retry |
| Existing damaged manifest, unreadable storage, or zero exit without valid results | Retain occupied state for inspection |
| Missing stop proof or surviving execution group | Retain occupied state; do not infer exit from files, timeouts or heartbeats |

Failure finalization rechecks identity, current dispatcher generation, latest
attempt, processing state, nonzero stop evidence and absence of database output
records under `BEGIN IMMEDIATE`. Both failed states commit together. Competing or
repeated identical finalizers acknowledge the same failure without rewriting its
timestamps. Failed commits roll back both states. Partial files are preserved;
failure does not itself launch another attempt. Filesystem absence is inspected
before that transaction, on trusted storage with no remaining invocation writer.

`failed`, `failure_denied`, `failure_unresolved` and `result_invalid` return CLI
exit 1; `idle`, `completed` and `recovered` return 0. Database contention returns
75. A recovery/failure outcome ends that cycle rather than launching another job.

## Processing flow

The caller supplies a trusted `ResultIdentity`, an already-loaded model session,
a database session factory, the upload directory and a local result store.
The caller must obtain execution permission first; constructing an identity is
not authorization. The caller also owns model creation and cleanup.

1. Read the queued job and its upload in a short database session, then close it.
2. Locate the stored WAV by its basename in application-owned upload storage.
   Reject path traversal, symlinks, non-regular files, oversized uploads and
   invalid or over-duration audio before inference.
3. Use the existing separation wrapper to expose exactly one input in a temporary
   directory and run the injected model into a separate temporary output directory.
4. Require exactly seven distinct files inside that directory: vocals, drums,
   bass, guitar, piano, other and instrumental. Reject duplicate manifest IDs,
   unexpected files, missing files and symlinks.
5. Check float32 WAV headers and require every stem's frame count, sample rate and
   channels to match the input. Move them to canonical names such as `guitar.wav`.
6. Let `LocalResultStore` copy and fully decode the WAVs, enforce byte/duration
   limits, record sizes and SHA-256 hashes, write the manifest last and verify the
   saved bundle. Temporary workspaces are removed on success and ordinary errors.

Inference never holds a database session. This function does not change job or
attempt status, insert output rows, report worker exit or retry inference.
It returns `VerifiedBundle`; publication remains a separate guarded database
transaction after confirmed worker exit.

An exception is propagated to the caller. No manifest means no ready result.
A failed store write can leave partial durable artifacts; these are not published
and follow the existing create-only result-store rules. Termination can bypass
Python cleanup, so host temporary-file housekeeping is still an operational task.
The application-owned storage directories are trusted local storage, not a
sandbox for an untrusted process running as the same OS user.

## Real result policy

`get_model_result_profile()` binds the pinned BS-RoFormer-SW inference revision,
seven expected stems, float32 output contract and configured maximum duration.
Its ID is separate from the fake profile. Callers must pass this trusted profile
when verifying real results; a manifest does not choose its own verification
policy. A changed duration setting changes the policy ID, so outstanding results
need their original policy when reconciling across a configuration change.

The per-stem byte budget is:

```text
maximum duration × 96,000 samples/second × 2 channels × 4 bytes + 64 KiB
```

The total budget is seven times that amount. Float32 outputs can be larger than
the original upload. These bounds constrain an individual bundle, not host-wide
disk usage or retention. At the default 600 seconds, the conservative bundle
bound is approximately 3.0 GiB; staging and persistent copies need additional disk.

The pinned upstream wrapper preserves the input sample rate and channels; this
slice does not introduce resampling. Layout tests at 48 kHz prove preservation,
not separation quality at that rate. The real smoke used 44.1 kHz. Decide and test
the model's accepted-rate/resampling policy before broadening beta audio claims.

## Verification on 2026-10-16

Focused tests use generated WAVs and an injected model that writes actual float32
files. They cover complete bundles larger and longer than the fake policy allows,
no database session during inference, unchanged publication state, mono/stereo
layouts, malformed outputs, invalid inputs and ordinary-error cleanup. The
existing CPU API test explicitly imports this module while blocking Torch and
BS-RoFormer imports. Inference remains optional for API-only environments.

Verification completed with 1,121 passing backend tests, Ruff lint/format checks
across all 92 maintained Python files, and a clean staged whitespace check.

One real run used a disposable SQLite database, temporary input/result storage
and an original four-second, 44.1 kHz stereo signal made from sine waves. No user
recordings or existing application records were used. Cached checkpoint assets
were checked without downloading; the model loader verified the checkpoint.

| Measurement | Observed |
| --- | --- |
| GPU | NVIDIA GeForce RTX 4060 Laptop GPU |
| Python / PyTorch | 3.12.3 / 2.13.0+cu130 |
| Model creation and load | 6.174 seconds |
| Inference plus bundle writing/verification | 6.983 seconds |
| Peak PyTorch CUDA allocation | 2,422.8 MiB |
| Output files | 7 float32 WAVs, 176,400 frames each |
| Bytes per output / total | 1,411,288 / 9,879,016 |

The bundle verified again after model cleanup, the original upload hash stayed
unchanged, and the database still had a pending job with no output rows. Disposable
storage was removed after the check. This is one processing-boundary smoke, not
an end-to-end queued-worker, long-song, concurrency or audio-quality benchmark.
Timings exclude interpreter startup and earlier imports and are not an SLA.

## Dispatcher verification on 2026-10-17

The actual `local-model` CLI completed a four-second original synthetic stereo
WAV on the RTX 4060 Laptop GPU, using a freshly migrated disposable SQLite
database and separate temporary audio storage. The entire dispatcher process,
worker startup, model loading, inference, exit confirmation and publication took
11.556 seconds. All seven outputs totaled 9,879,016 bytes. The recorded child PID
was gone, its saved exit code was zero, its attempt was succeeded, and seven
output rows and the completed job were visible together. The input hash was
unchanged. A second cycle was idle and created no extra launch log.

This exercised actual descriptor-level log separation: model diagnostics appeared
in the invocation log and the dispatcher stdout remained a single JSON reply.
Both the database and audio were removed after the check; no existing user data
was used. The measurement is one short same-host smoke, not a throughput or
long-song benchmark. The real service checks below additionally cover API restart
and interrupted real inference.

CPU model substitutes also exercise actual child processes without importing
Torch, including failure before results, failure during cleanup after saved
results, killed workers, duplicate delivery and failed stop-proof commits.
Guarded database tests run against both ORM-created and Alembic-migrated schemas,
including competing failure finalizers and transaction rollback. The targeted
regression group passed 233 tests; the complete backend suite passed 1,178 tests
in 291.50 seconds. Browser history passed all nine existing tests. JavaScript
syntax, Ruff lint/format across 97 maintained Python files, and whitespace checks
also passed.

The rebuilt `isojam-api:model-worker-check` CPU image passed all 1,178 tests again
under hash-locked dependencies as UID/GID 10001, with Python 3.12.14 and no Torch
installed (366.05 seconds). Both existing container smokes passed: disabled API
data survived replacement, and queued fake execution recovered intentionally
interrupted publication, then served all seven owned WAVs with exact hashes and
preserved keyed submission replay. Disposable labeled containers/volumes were
cleaned up by the scripts. This validates the CPU API image; it does not package
the GPU environment into that image.

## Real service verification on 2026-10-17

From the activated CUDA environment in `backend`, run:

```bash
python scripts/smoke_model_worker.py
```

The script checks cached assets without downloading, applies migrations to a
fresh temporary database, starts a loopback API and continuous dispatcher, and
uses original synthetic stereo 44.1 kHz WAVs. It restarts the API during a
20-second recording's actual inference, preserving the same attempt, invocation
and worker PID. All seven downloaded files match the verified bundle hashes;
the original upload bytes, owning token, cross-account denial and keyed submission
replay survive API replacement.

It then kills only its own worker during another 20-second recording's inference.
The controller records signal exit -9, atomically marks the job/attempt failed
and does not retry it. The same live dispatcher completes a later four-second
recording. There are exactly three jobs and three attempts, two completed jobs
with fourteen output rows, and one failed job. The dispatcher finally handles
SIGTERM and exits zero. Three runs of this real scenario passed; the final run
also verified process-descriptor-based signalling of the disposable worker.

All services, database/audio data and worker scratch space are scoped to the
smoke's temporary directory and cleaned up. This test signals a worker through a
Linux process descriptor only after checking its `/proc` environment points to
that disposable database, avoiding a signal to a reused numeric PID. It never
uses an existing recording or application database. Worker crashes outside this
isolated smoke can leave ordinary temporary workspaces for host housekeeping.

CPU tests cover polling/backoff, duplicate-report suppression, later job discovery,
SIGTERM/SIGINT draining with a real child, exclusion of the next job, competing
expired reservation takeover, generation fencing and refusal to take over
submission/ambiguous state. The systemd example passed `systemd-analyze verify`
using a temporary copy with workspace-specific account/path substitutions; it was
not installed or enabled.

The complete backend suite passed all 1,205 tests locally in 366.50 seconds and
again in the rebuilt CPU image with locked dependencies as UID/GID 10001 in
459.90 seconds. The container run reported fifteen existing SQLAlchemy
`Result.tuples()` deprecation warnings in job-history tests. Ruff lint/format
checks passed across all 99 maintained Python files. Both container smokes also
passed again: disabled-mode data survived replacement, and queued fake execution
recovered interrupted publication with owned, hash-matched downloads and keyed
submission replay. All smoke-owned containers, volumes and temporary data were
removed. These checks do not package the GPU worker into the CPU image.

## Remaining release work

The [local GPU stack](local-stack.md) packages this worker in a separate image
and runs it beside the CPU API with Compose. The dispatcher and child workers
remain in the same processor container for process-group supervision.

Establish the accepted-rate/resampling policy before claiming broad input-rate
quality. Hosted deployment, disk/log retention and
operator handling of unknown execution remain separate work. Continuous polling
does not turn unknown termination into a safe inference retry.

General lesson: a model returning successfully is only one step in a job's
lifecycle. Validate its artifacts, persist the evidence, confirm execution ended
and publish the database state as separate operations with explicit contracts.
