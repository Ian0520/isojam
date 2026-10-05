# Operations and troubleshooting

These notes cover the current single-host CPU API container and local GPU demo.
For a complete container walkthrough, use [containers.md](containers.md). Do not
apply destructive experiments to a volume holding data you want to keep.

## Check the running CPU API

Use the container name and port selected when starting your own server. The
examples below match the manual container guide, not disposable smoke names.

```bash
curl --fail http://127.0.0.1:8000/health
docker ps --filter name=isojam-api
docker logs --tail 100 isojam-api
docker stats --no-stream isojam-api
docker exec isojam-api id
docker volume inspect isojam-data
```

`/health` means the application started and can answer HTTP. It does not prove a
worker is running, the GPU is healthy, or a job will finish. Check a known job's
status and authenticated output access for those flows. Inspect logs locally;
avoid sharing signing secrets, passwords, tokens or users' recordings.

`docker stats` reports this container's current resources. Record workload,
mode, input duration and cold/warm state alongside measurements. The measured
GPU demo in [demo.md](demo.md) runs outside this CPU container, so its memory
numbers cannot be used to estimate the CPU image's requirements.

## Replace a container without losing data

Application source is packaged into the image. Build a new tagged image, then
create a new container with the same named volume and runtime settings. Starting
the old container runs its old image. Removing a container does not remove an
explicitly named volume; data in its writable layer has no such protection.

Before a release, save a consistent copy of both SQLite metadata and audio. Stop
all writers, including the API and dispatcher, before copying database files and
recordings. A live file copy can miss an active SQLite journal or WAL. A volume
is persistent storage, **not a backup**. Recovery requires a tested restore copy.

Apply Alembic migrations as a deliberate one-off operation using the same volume,
paths and settings as the API, then start the new server and verify health plus
an existing result. The [container guide](containers.md) has the exact commands.
A previous image tag is useful for application rollback, but an older application
may not understand a newer schema. Plan compatible migrations and restore options;
changing an image alone does not undo data migrations.

## Diagnose common problems

| Symptom | Check and next action |
| --- | --- |
| Docker commands only show a client | Start Docker Desktop, use Linux containers and enable the Ubuntu WSL integration. |
| Startup fails before serving HTTP | Read logs for missing/blank JWT secret, invalid paths or limits, or un-applied migrations. Keep the same signing secret across replacements. |
| Local startup cannot load the model | Confirm the inference extra is installed in the active environment, CUDA is available, and the GPU environment matches the model spike. The CPU image deliberately has no model. |
| New jobs return 503 | Check `ISOJAM_PROCESSING_MODE`. The CPU image defaults to `disabled`; use local GPU mode for real separation or queued mode for the fake exercise. |
| Browser says Demo queue | It is queued mode. Run an explicit fake dispatcher cycle against the same database and audio directory. Its WAVs are test artifacts. |
| Upload returns 413 / 422 | Check byte/duration limits or readable mono/stereo WAV content. Renaming a file is insufficient; the server decodes and validates it. |
| Browser sign-in expires | Log in again and reopen the saved job ID. Access tokens expire after 30 minutes; there is no refresh-token flow. |
| Job submission returns 429 | The user has reached the configured unfinished-job allowance. Inspect existing jobs before creating more work. |
| Another account receives 404 | Ownership checks intentionally hide the resource. Use the owning account; do not weaken the check. |
| Pending queued job never starts | A cycle may not have run, or the single global attempt slot is occupied. Inspect the dispatcher JSON result. |
| Stopped fake worker has saved outputs but job is still processing | Run `python -m app.dispatcher --once --adapter local-fake --reconcile-only` with matching storage/database settings. Valid saved stop proof and the entire verified bundle are required. |
| Dispatcher reports `database_busy` | Another SQLite writer held the bounded lock wait. Diagnose the writer and retry the cycle; this is not permission to launch another worker. |
| Exit is unknown or bundle is invalid | Keep the attempt occupied and inspect evidence. Heartbeat age, timeout, file presence or a duplicate child's exit cannot safely authorize replacement. |
| Local inference was interrupted | Local FastAPI background work is not recovered. Preserve the job as evidence; restarting the API does not resume it. Durable real-worker recovery is future work. |

The current queue prioritizes avoiding duplicate execution over automatically
recovering every failure. Publication recovery is implemented. Automatic failure
resolution and safe retries of uncertain real executions are not.

## Reproduce a failure safely

Use the [CI commands](ci.md#run-the-same-checks-locally) for disposable checks.
The queued smoke deliberately interrupts publication, replaces the API and
reconciles the original stopped attempt. It verifies exact hashes and IDs rather
than accepting a second execution as recovery. These scripts create uniquely
named resources with ownership labels and clean up only their own resources.

Keep a short incident record: input characteristics, processing mode, image or
commit, last observed job state, exact failure, logs and what action restored the
flow. Avoid assuming that a successful HTTP response proves processing success.
