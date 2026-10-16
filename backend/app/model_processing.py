"""Real inference and durable results for an already-authorized queued invocation."""

import stat
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory

import soundfile as sf
from sqlalchemy.orm import Session

from app.audio import validate_wav
from app.config import get_max_audio_duration_seconds, get_max_upload_bytes
from app.repositories import jobs, uploads
from app.results import (
    LocalResultStore,
    ResultIdentity,
    ResultProfile,
    ResultValidationError,
    VerifiedBundle,
)
from app.separation import separate_audio

MODEL_STEMS = ("vocals", "drums", "bass", "guitar", "piano", "other", "instrumental")


def get_model_result_profile() -> ResultProfile:
    """Bind the pinned model/output contract and configured duration to a policy."""
    duration = get_max_audio_duration_seconds()
    # The wrapper preserves the input rate/channels and writes float32 WAVs.
    # Allow the largest accepted input layout plus bounded WAV header overhead.
    max_stem_bytes = duration * 96000 * 2 * 4 + 64 * 1024
    return ResultProfile(
        id=f"bs-roformer-sw-de35ada5817b-float32-{duration}s-v1",
        required_stems=MODEL_STEMS,
        max_stem_bytes=max_stem_bytes,
        max_total_bytes=len(MODEL_STEMS) * max_stem_bytes,
        max_duration_seconds=duration,
    )


def _input_path(
    identity: ResultIdentity,
    db_session_factory: Callable[[], Session],
    upload_dir: Path,
) -> Path:
    with db_session_factory() as session:
        job = jobs.get_job(session, identity.job_id)
        if job is None:
            raise KeyError("job not found")
        if job.execution_backend != "queued":
            raise ValueError("model worker requires a queued job")
        upload = uploads.get_upload(session, job.upload_id)
        if upload is None:
            raise RuntimeError("upload not found")
        filename = upload.stored_filename

    # Uploads are application-owned files on this host. Never interpret a stored
    # filename as an arbitrary path, or follow a symlink supplied in its place.
    if Path(filename).name != filename or "\\" in filename:
        raise ValueError("stored upload filename must be a basename")
    path = upload_dir.resolve() / filename
    if path.suffix.lower() != ".wav" or not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("stored upload must be a regular WAV file")
    if path.stat().st_size > get_max_upload_bytes():
        raise ValueError("stored upload exceeds its byte limit")
    return path


def _stage_outputs(
    paths: dict[str, Path],
    raw_dir: Path,
    workspace: Path,
    input_info,
    profile: ResultProfile,
) -> None:
    if set(paths) != set(profile.required_stems):
        raise ResultValidationError(
            "Model must return exactly the seven required stems"
        )
    if len(set(paths.values())) != len(paths):
        raise ResultValidationError("Model stems must refer to distinct files")
    if any(path.parent != raw_dir for path in paths.values()):
        raise ResultValidationError("Model output must stay inside its workspace")
    if set(raw_dir.iterdir()) != set(paths.values()):
        raise ResultValidationError("Model workspace contains missing or extra outputs")

    expected = (input_info.frames, input_info.samplerate, input_info.channels)
    for path in paths.values():
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ResultValidationError("Model output must be a regular file")
        if not 0 < metadata.st_size <= profile.max_stem_bytes:
            raise ResultValidationError(
                "Model output exceeds its byte limit or is empty"
            )
        try:
            info = sf.info(path)
        except sf.SoundFileError as error:
            raise ResultValidationError(
                "Model output must contain WAV audio"
            ) from error
        if info.format not in {"WAV", "WAVEX"} or info.subtype != "FLOAT":
            raise ResultValidationError("Model output must be a float32 WAV")
        if (info.frames, info.samplerate, info.channels) != expected:
            raise ResultValidationError(
                "Model output must match the input audio layout"
            )

    for stem, path in paths.items():
        path.rename(workspace / f"{stem}.wav")


def process_model_job(
    identity: ResultIdentity,
    model_session,
    db_session_factory: Callable[[], Session],
    *,
    upload_dir: Path,
    result_store: LocalResultStore,
) -> VerifiedBundle:
    """Produce verified files; the caller owns execution authority and the model.

    No job/attempt status or output rows are changed here. The worker must obtain
    execution permission before calling this function; publication additionally
    requires confirmed worker exit. Inference holds no database session open.
    """
    profile = get_model_result_profile()
    input_path = _input_path(identity, db_session_factory, upload_dir)
    validate_wav(input_path, max_duration_seconds=profile.max_duration_seconds)
    input_info = sf.info(input_path)

    with TemporaryDirectory(prefix="isojam-model-") as temporary:
        root = Path(temporary)
        raw_dir = root / "model"
        workspace = root / "bundle"
        workspace.mkdir()
        paths = separate_audio(input_path, raw_dir, model_session)
        _stage_outputs(paths, raw_dir, workspace, input_info, profile)
        # The store decodes all samples, enforces byte/duration budgets, hashes
        # persisted files and writes the manifest last, then verifies it again.
        return result_store.write_bundle(identity, workspace, profile=profile)
