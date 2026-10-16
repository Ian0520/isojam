# Real model worker

## Current boundary

The first slice on `feat/model-worker` adds `app.model_processing.process_model_job`.
It produces verified files for an already-authorized queued invocation. Dispatcher
and worker CLI integration are not implemented in this slice: the queued adapter
still runs the fake worker, and local API processing keeps its existing behavior.

For the initial same-host release, retain the SQLite queue and start with one
active inference subprocess. This reuses the existing reservation, execution and
publication rules. It also leaves their maintenance with this project. Adopting
Celery remains a later decision, rather than combining a queue migration with
model integration. A separate process per job allows confirmed exit before
publication, at the cost of loading the model for each job.

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

## Next slices

Connect an authorized real worker and an explicit adapter to the dispatcher.
Keep model diagnostics separate from the bounded control reply, replace the
fake worker's short timeout with an inference-appropriate limit, establish actual
process-lifetime/termination guarantees, and distinguish confirmed failure from
unknown execution before releasing occupied capacity. Add the supervised service
loop and test real completion, API restart and worker interruption end to end.

General lesson: a model returning successfully is only one step in a job's
lifecycle. Validate its artifacts, persist the evidence, confirm execution ended
and publish the database state as separate operations with explicit contracts.
