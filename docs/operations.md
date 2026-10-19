# Operations and troubleshooting

These notes use the recommended [local Compose stack](local-stack.md). Start
with the [README quickstart](../README.md#start-locally-with-docker-compose) for
installation and account creation. The [standalone CPU container guide](containers.md)
is a separate exercise with different container names and storage.

## Inspect the local Compose stack

From the repository root, with Docker running:

```bash
docker compose ps -a
docker compose logs --tail 100 migrate processor api
curl --fail http://127.0.0.1:8000/health
```

Use the port configured by `ISOJAM_PORT` if it differs from 8000. A successful
`migrate` container exits with code 0; the API and processor should remain running
and become healthy. For live logs or a resource snapshot:

```bash
docker compose logs --follow api processor
docker compose stats --no-stream api processor
```

`/health` proves that the API can answer HTTP. It does not establish that a worker
is running, CUDA is usable, or a particular job will finish. The processor's
startup preflight checks GPU access and cached assets, and its lightweight health
probe checks dispatcher liveness. Neither measures successful inference or queue
throughput. Check an actual recording and its owned outputs for the whole flow.

Container memory figures are not GPU VRAM usage. Record the workload, mode,
input duration and cold/warm state alongside measurements. The historical native
GPU measurement in [demo.md](demo.md#one-measured-local-run) includes a model in
the API process and cannot size the separate CPU container.

Inspect logs locally before sharing them; keep signing secrets, passwords,
tokens and personal recordings out of incident reports.

## Stop and replace services

For a normal stop, press Ctrl+C once in the foreground Compose terminal, or run
`docker compose stop` from another terminal. Wait for the active cycle to drain.
The API stops first; the processor gets up to 35 minutes to finish its default
30-minute worker deadline and publication. A second Ctrl+C or forced kill can
interrupt inference and leave an attempt needing inspection.

After updating source, stop the existing services before applying a new build:

```bash
docker compose stop
docker compose up --build --abort-on-container-failure
```

The stack applies migrations before starting the processor and API. Stopping
first prevents a future migration from running while old services still write.
Named data/model volumes and the same `.env` preserve accounts, recording paths,
results and cached assets. Restarting an old container uses its old image;
configuration changes require recreation with the updated Compose environment.

`docker compose down` removes containers and the network while keeping named
volumes. Adding `--volumes` deletes the database, recordings, outputs and cached
models. Changing the Compose project name also selects a separate set of volumes.
Do not use either as a way to fix startup failures involving personal data.

For manual `docker run` replacements, follow the separate
[CPU container instructions](containers.md) and retain that exercise's named
volume, paths and runtime settings.

## Data and backups

A persistent volume is not a backup. No automated retention, backup or restore
procedure is implemented. Before a migration or other data operation, preserve a
consistent copy of SQLite metadata and audio, and verify the restore separately.
Stop all writers, including API and processor, before copying database files and
recordings. A live copy can omit an active SQLite journal or WAL.

An old image can help roll back application code, but it may not understand a
newer schema. Changing an image does not undo migrations. Plan schema compatibility
and data restore together. Invocation logs and audio inside the data volume do
not rotate automatically; Docker stdout/stderr logs rotate separately.

## Diagnose common problems

| Symptom | Check and next action |
| --- | --- |
| `docker` is missing in Ubuntu, or only Client information appears | Start Docker Desktop, use Linux containers, enable Ubuntu WSL integration, then retry `docker version` from Ubuntu. |
| Compose refuses a missing JWT secret | Copy `.env.example` only if needed and set a generated secret in root `.env`. Do not replace an existing secret during routine startup. |
| Startup fails before HTTP is available | Read `migrate`, `processor` and `api` logs. Fix the first failed dependency; later services may be correctly waiting for it. |
| Migrations exited with code 0 | This is expected. The oneshot service finishes before processor/API startup. |
| Port 8000 is already allocated | Stop the old service or set a free `ISOJAM_PORT` in `.env`, recreate the stack, and open that port in the browser. |
| Build fails with disk-space errors | Check `df -h / /mnt/c` in WSL. Linux virtual capacity alone can hide a full Windows host drive. Preserve volumes and recordings when reclaiming space. |
| Processor reports absent or invalid cached model files | Stop the stack and run `docker compose run --rm models` to prepare and verify the cache. Inspect any remaining hash-mismatch error before restarting. Normal processor startup never downloads assets. |
| Processor cannot access CUDA | Check Docker GPU support and the NVIDIA host driver against the locked CUDA runtime. The CPU API image contains no model; native Python installs do not alter the GPU container. |
| New jobs return 503 | Check the processing mode. The standalone CPU image defaults to `disabled`; Compose explicitly uses `queued` and starts a processor. Native setup needs its own model/dispatcher configuration. |
| API reports separation availability but jobs stay queued | That label reflects API mode, not live GPU readiness. Check processor health/logs. The single global slot may be held by an unfinished or uncertain attempt; do not start competing workers to bypass it. |
| Real model worker fails | Inspect the invocation log under `worker-logs` in audio storage. Check CUDA, cached assets and input characteristics. A confirmed nonzero exit without a manifest becomes failed; inference is not automatically retried. |
| Upload returns 413 / 422 | Check upload size, converted size, duration and readable mono/stereo content. Supported files are WAV, MP3, M4A, AAC and FLAC; renaming a file does not convert its contents. |
| Upload returns 503 | Conversion may be busy; retry after current uploads finish. For missing conversion tools, rebuild the API image or install FFmpeg/ffprobe in the native API environment. |
| Browser sign-in expires | Sign in again and reopen the recording from **Recent recordings** or **Find by job ID**. Tokens expire after 30 minutes; there is no refresh-token flow. |
| Create account is hidden or registration returns 403 | Check `ISOJAM_REGISTRATION_MODE`. For local signup use `open`, recreate the API, then reload. In `invite` mode, use a valid code for the exact invited email. Closing registration preserves existing accounts and login. |
| Submission returns 429 | The account reached its unfinished-job allowance. Inspect pending/processing work before submitting more. |
| Another account receives 404 | Ownership checks intentionally hide resources. Use the owning account. |
| Native dispatcher stops when its terminal closes | Use `--serve` under a supervisor as described in the worker guide. Compose already supervises its dispatcher inside `processor`; its containers deliberately have no automatic restart policy. |
| Shutdown takes time during inference | SIGTERM/SIGINT drains the active cycle before exit. Let it finish; a stale heartbeat does not prove execution stopped. |
| An expired reservation blocks work after a controller restart | Polling may reclaim an expired owned `reserved` attempt with a new generation. `submitting` and later states require reliable execution evidence. |
| Saved results exist but the job still appears processing | Reconcile with the matching worker adapter and database/audio paths. Valid saved stop proof and the entire verified bundle are required; see the worker guide. |
| Dispatcher reports `database_busy` | Another SQLite writer exceeded the bounded lock wait. Diagnose that writer and retry the cycle; this does not permit an additional worker launch. |
| Worker exit is unknown or its bundle is invalid | Keep the attempt occupied and inspect evidence. Heartbeat age, a timeout, file presence or a duplicate child's exit cannot authorize replacement. |
| Native `local` inference was interrupted | FastAPI background work is not recovered on restart. Preserve the job for inspection. The separate queued worker can reconcile saved stop evidence and results; it cannot invent missing proof. |
| Browser shows old colors/layout or `Cannot set properties of null` | Rebuild after source changes, then reload. An older script can refer to an element removed from newer HTML. Use Ctrl+Shift+R once to clear assets cached before the revalidation fix. See below. |

The queue prioritizes avoiding duplicate execution over recovering every failure
automatically. Publication recovery is implemented. Automatic failure resolution
and safe retries of uncertain real executions are not. Consult
[real worker recovery](model-worker.md) before changing an attempt's state.

## Browser assets after an update

The HTML and static CSS/JavaScript responses use `Cache-Control: no-cache`.
Browsers may retain the bytes, but must check freshness before reusing them.
Unchanged files can still receive an ETag-based `304 Not Modified` response.
Versioned entry/module URLs also bypass assets cached before this policy existed.

A browser refresh cannot copy changed files into an existing image. Rebuild and
replace containers first. If the old interface persists, hard-refresh once.
Signing in again may be needed if the tab's 30-minute access token has expired;
a UI update does not remove the account or recordings.

## Reproduce a failure safely

Use the [CI commands](ci.md#run-the-same-checks-locally) for disposable checks.
The queued smoke interrupts publication, replaces the API and reconciles the
original stopped attempt. It verifies exact hashes and IDs, so a second execution
cannot pass as publication recovery. The scripts use uniquely named, labeled
resources and clean up only what they own. The real GPU Compose smoke is
separate from CPU CI; see [local stack verification](local-stack.md#disposable-verification).

Keep an incident record with input characteristics, processing mode, image or
commit, last observed job state, failure, relevant logs and the action that
restored operation. A successful HTTP response alone does not prove processing
success.
