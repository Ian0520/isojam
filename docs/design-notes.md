# Design notes

IsoJam combines an authenticated audio API, a browser studio and local model
inference. A separate queued fake-worker path exercises execution coordination,
result verification and publication recovery. These notes explain the boundaries
and tradeoffs of those components.

## Request flow

Start in `backend/app/routers/jobs.py` and `backend/app/static/app.js`.

1. A user logs in; the browser sends a bearer access token on protected requests.
2. The server authenticates the user, validates the WAV and stores owned upload metadata.
3. The browser creates an idempotency key before submitting a job. Job and receipt
   persist together; replaying the same accepted intent returns the existing job.
4. Local mode schedules the real model as a background task. Queued mode only
   accepts work for a separate dispatcher; it does not load the model.
5. The browser polls state. A completed job exposes owner-checked stem routes.
   Playback fetches authenticated bytes and uses a temporary blob URL because
   an ordinary audio element does not attach our bearer header.

**General lesson:** a network error does not tell the client whether the server
accepted a command. The client must preserve an intent identifier before sending
it, and the server must store its receipt atomically with the resulting change.

## Container operation

Read `backend/Dockerfile`, `.dockerignore`, `backend/scripts/check_container.py`
and `backend/scripts/smoke_container.py`.

- The Dockerfile is the build recipe. An image contains packaged code and runtime;
  a container is an instance with its own process and writable layer.
- Runtime secrets are supplied outside the image. The named volume holds metadata
  and audio across container replacement. Persisting data is different from backing it up.
- Alembic runs once as a separate migration operation against the same storage.
- New source requires a new image and container; restarting the old one does not
  adopt source changes. An old image cannot necessarily use a newly migrated schema.
- Tests run as UID/GID 10001 in a disposable container. Test dependencies do not
  enter the runtime image, and real project data is not used by the checks.

**General lesson:** reproducible packaging, runtime configuration and durable
state have different lifecycles. A reliable release must account for all three.

## Reliability boundary

Read `backend/app/dispatcher.py`, `backend/app/fake_worker.py`,
`backend/app/results.py` and `backend/app/repositories/job_reservations.py`.

A job describes the user's requested work. An attempt identifies one execution
cycle. A dispatcher reservation grants temporary coordination authority; a worker
execution grant is a separate permission to start processing. Generation checks
fence stale dispatchers. Job-submission receipts prevent duplicate job creation;
they do not replace attempts or guarantee successful execution.

A heartbeat reports contact, not completion or termination. The local controller
waits for the exact authorized child and saves stop proof. The result manifest
identifies the attempt and records all required files, sizes and hashes. Both
confirmed stop and a verified complete bundle are needed before publication.
All output metadata and terminal states commit together. A fresh controller can
recover missing publication from those saved facts without re-running the worker.

**General lesson:** separate performing work from publishing its result. A failed
publication can be retried safely when durable evidence already establishes the
execution and complete outputs. Unknown execution cannot be replaced merely
because a heartbeat got old. This prioritizes avoiding duplicate costly work over
automatic progress in every failure case; it is not a universal exactly-once claim.

## Design review questions

1. Where is ownership checked for uploads, jobs and downloads? Why is cross-account
   access reported as 404, and authentication failure as 401?
2. What happens if the job POST succeeds but its response is lost? What survives
   API replacement, and what retry state is only held in the browser?
3. Why do a named volume, database migration and image rebuild solve different problems?
4. Why are request-body limits, decoded-audio validation and job-admission limits
   all needed? Which one actually limits concurrent GPU inference today?
5. What proves an execution stopped? Why do valid files alone not prove that?
6. Which facts must commit atomically when publishing seven stems? What would a
   second recovery controller see after the first one completes?
7. Which paths are covered by fake tests, container HTTP checks and the real GPU
   demo? What important behavior remains untested or unimplemented?
8. If adding Redis, which expendable data would be cached, what would its TTL be,
   and how would the API behave on a cache miss or Redis failure? Why must receipts
   and ownership remain authoritative database records?

## Future development

Connect real GPU execution to the durable queue, add safe resolution of uncertain
executions, and test backup/restore before deploying a hosted service. A hosted
release also needs runtime secrets, monitoring and explicit resource limits.

If caching is added, first define which data is expendable, how long it remains
valid, and how misses or cache failures behave. Database receipts and ownership
checks must remain authoritative. Migrating SQLite to another database requires
reviewing transaction isolation and locking assumptions in the repositories.
