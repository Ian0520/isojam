from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pytest
import soundfile as sf
from sqlalchemy import select

from app.db_models import Job, JobOutput, Upload
from app.model_processing import (
    MODEL_STEMS,
    get_model_result_profile,
    process_model_job,
)
from app.results import FAKE_RESULT_PROFILE, LocalResultStore, ResultIdentity
from app.results import ResultValidationError as InvalidResult
from tests.factories import create_test_upload, create_test_user, make_wav_bytes


class WritingModel:
    """Match the real package's file naming, float32 encoding and manifest shape."""

    def __init__(self, *, stems=MODEL_STEMS, subtype="FLOAT", transform=None):
        self.stems = stems
        self.subtype = subtype
        self.transform = transform or (lambda audio: audio)
        self.called = False

    def infer(self, input_folder, *, store_dir):
        self.called = True
        self.store_dir = store_dir
        inputs = list(input_folder.iterdir())
        assert len(inputs) == 1
        self.input_path = inputs[0]
        data, rate = sf.read(self.input_path, always_2d=True, dtype="float32")
        outputs = []
        for stem in self.stems:
            path = store_dir / f"{self.input_path.stem}_{stem}.wav"
            sf.write(path, self.transform(data), rate, subtype=self.subtype)
            outputs.append(SimpleNamespace(output_id=stem, output_path=path))
        return SimpleNamespace(outputs=outputs)


@pytest.fixture
def model_case(tmp_path, test_session_factory):
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    path = upload_dir / "stored.wav"
    path.write_bytes(make_wav_bytes(frames=2 * 44100))
    with test_session_factory() as session:
        user = create_test_user(session)
        upload = create_test_upload(session, user, stored_filename=path.name)
        job = Job(upload_id=upload.id, status="pending", execution_backend="queued")
        session.add(job)
        session.flush()
        identity = ResultIdentity(
            job_id=job.id, attempt_id=uuid4(), invocation_id=uuid4()
        )
        upload_id = upload.id
        session.commit()
    return SimpleNamespace(
        identity=identity,
        upload_id=upload_id,
        input_path=path,
        upload_dir=upload_dir,
        store=LocalResultStore(tmp_path / "results"),
        factory=test_session_factory,
    )


def process(case, model):
    return process_model_job(
        case.identity,
        model,
        case.factory,
        upload_dir=case.upload_dir,
        result_store=case.store,
    )


def assert_unpublished(case):
    assert not (case.store.root / case.store.manifest_key(case.identity)).exists()
    with case.factory() as session:
        assert session.get(Job, case.identity.job_id).status == "pending"
        assert not session.scalars(select(JobOutput)).all()


def test_model_job_saves_real_profile_bundle_without_publishing(model_case):
    case = model_case
    original = case.input_path.read_bytes()
    model = WritingModel()
    active_sessions = []
    original_factory = case.factory

    @contextmanager
    def tracked_factory():
        with original_factory() as session:
            active_sessions.append(session)
            try:
                yield session
            finally:
                active_sessions.remove(session)

    original_infer = model.infer

    def infer(input_folder, *, store_dir):
        assert not active_sessions, "Inference must not hold a database session"
        return original_infer(input_folder, store_dir=store_dir)

    model.infer = infer
    case.factory = tracked_factory
    bundle = process(case, model)

    assert bundle == case.store.verify_bundle(
        case.identity, profile=get_model_result_profile()
    )
    assert bundle.manifest.profile_id != FAKE_RESULT_PROFILE.id
    assert {artifact.stem for artifact in bundle.outputs} == set(MODEL_STEMS)
    for artifact in bundle.outputs:
        assert artifact.byte_count > FAKE_RESULT_PROFILE.max_stem_bytes
        info = sf.info(artifact.path)
        assert info.frames == 88200
        assert info.subtype == "FLOAT"
        assert artifact.path.name == f"{artifact.stem}.wav"
    assert case.input_path.read_bytes() == original
    assert not model.store_dir.parent.exists()
    assert not model.input_path.parent.exists()
    with case.factory() as session:
        assert session.get(Job, case.identity.job_id).status == "pending"
        assert not session.scalars(select(JobOutput)).all()
    with pytest.raises(InvalidResult, match="identity/profile"):
        case.store.verify_bundle(case.identity)  # Fake remains the explicit default.


@pytest.mark.parametrize("channels,rate", [(1, 44100), (2, 48000)])
def test_model_job_preserves_mono_or_stereo_layout(model_case, channels, rate):
    model_case.input_path.write_bytes(
        make_wav_bytes(frames=rate, sample_rate=rate, channels=channels)
    )

    bundle = process(model_case, WritingModel())
    for artifact in bundle.outputs:
        info = sf.info(artifact.path)
        assert (info.frames, info.samplerate, info.channels) == (rate, rate, channels)


@pytest.mark.parametrize("stems", [MODEL_STEMS[:-1], (*MODEL_STEMS, "unknown")])
def test_model_job_rejects_incomplete_or_extra_stems(model_case, stems):
    with pytest.raises(InvalidResult, match="seven required stems"):
        process(model_case, WritingModel(stems=stems))
    assert_unpublished(model_case)


@pytest.mark.parametrize(
    "transform",
    [lambda audio: audio[:-1], lambda audio: audio[:, :1]],
    ids=["short-stem", "wrong-channels"],
)
def test_model_job_rejects_layout_mismatch(model_case, transform):
    with pytest.raises(InvalidResult, match="input audio layout"):
        process(model_case, WritingModel(transform=transform))
    assert_unpublished(model_case)


def test_model_job_rejects_wrong_encoding(model_case):
    with pytest.raises(InvalidResult, match="float32 WAV"):
        process(model_case, WritingModel(subtype="PCM_16"))
    assert_unpublished(model_case)


def test_model_job_rejects_wrong_sample_rate(model_case):
    class WrongRateModel(WritingModel):
        def infer(self, input_folder, *, store_dir):
            manifest = super().infer(input_folder, store_dir=store_dir)
            path = manifest.outputs[0].output_path
            audio, rate = sf.read(path)
            sf.write(path, audio, rate + 1, subtype="FLOAT")
            return manifest

    with pytest.raises(InvalidResult, match="input audio layout"):
        process(model_case, WrongRateModel())
    assert_unpublished(model_case)


def test_model_job_rejects_nonfinite_samples(model_case):
    with pytest.raises(InvalidResult, match="invalid WAV audio"):
        process(model_case, WritingModel(transform=lambda audio: audio * np.nan))
    assert_unpublished(model_case)


@pytest.mark.parametrize(
    "problem", ["outside", "symlink", "duplicate-path", "extra-file", "missing-file"]
)
def test_model_job_rejects_unexpected_output_paths(model_case, problem):
    class InvalidModel(WritingModel):
        def infer(self, input_folder, *, store_dir):
            manifest = super().infer(input_folder, store_dir=store_dir)
            first = manifest.outputs[0]
            if problem == "outside":
                first.output_path = model_case.input_path
            elif problem == "symlink":
                first.output_path.unlink()
                first.output_path.symlink_to(model_case.input_path)
            elif problem == "duplicate-path":
                first.output_path = manifest.outputs[1].output_path
            elif problem == "extra-file":
                (store_dir / "unexpected.txt").write_text("unexpected")
            else:
                first.output_path.unlink()
            return manifest

    with pytest.raises(InvalidResult):
        process(model_case, InvalidModel())
    assert_unpublished(model_case)


def test_model_job_cleans_temporary_outputs_after_model_failure(model_case):
    class FailingModel(WritingModel):
        def infer(self, input_folder, *, store_dir):
            super().infer(input_folder, store_dir=store_dir)
            raise RuntimeError("inference failed")

    model = FailingModel()
    with pytest.raises(RuntimeError, match="inference failed"):
        process(model_case, model)
    assert not model.store_dir.parent.exists()
    assert model_case.input_path.exists()
    assert_unpublished(model_case)


@pytest.mark.parametrize("problem", ["missing", "invalid", "symlink", "too-long"])
def test_model_job_rejects_bad_input_before_inference(model_case, monkeypatch, problem):
    path = model_case.input_path
    if problem == "missing":
        path.unlink()
    elif problem == "invalid":
        path.write_bytes(b"not audio")
    elif problem == "symlink":
        target = path.with_name("other.wav")
        path.rename(target)
        path.symlink_to(target)
    else:
        monkeypatch.setenv("ISOJAM_MAX_AUDIO_DURATION_SECONDS", "1")
    model = WritingModel()
    with pytest.raises((ValueError, FileNotFoundError)):
        process(model_case, model)
    assert not model.called
    assert_unpublished(model_case)


def test_model_job_rejects_stored_path_traversal(model_case):
    with model_case.factory() as session:
        session.get(Upload, model_case.upload_id).stored_filename = "../outside.wav"
        session.commit()
    model = WritingModel()
    with pytest.raises(ValueError, match="basename"):
        process(model_case, model)
    assert not model.called
    assert_unpublished(model_case)


def test_model_job_rejects_oversized_input_before_inference(model_case, monkeypatch):
    monkeypatch.setenv("ISOJAM_MAX_UPLOAD_BYTES", "100")
    model = WritingModel()
    with pytest.raises(ValueError, match="byte limit"):
        process(model_case, model)
    assert not model.called
    assert_unpublished(model_case)


def test_model_job_rejects_missing_upload_before_inference(model_case, monkeypatch):
    monkeypatch.setattr("app.model_processing.uploads.get_upload", lambda *_: None)
    model = WritingModel()
    with pytest.raises(RuntimeError, match="upload not found"):
        process(model_case, model)
    assert not model.called
    assert_unpublished(model_case)


def test_model_job_requires_existing_queued_job(model_case):
    with model_case.factory() as session:
        session.get(Job, model_case.identity.job_id).execution_backend = "local"
        session.commit()
    model = WritingModel()
    with pytest.raises(ValueError, match="queued job"):
        process(model_case, model)
    model_case.identity = model_case.identity.model_copy(update={"job_id": uuid4()})
    with pytest.raises(KeyError, match="job not found"):
        process(model_case, model)
    assert not model.called


def test_real_profile_binds_configured_duration_and_bounds_float32_outputs(monkeypatch):
    monkeypatch.setenv("ISOJAM_MAX_AUDIO_DURATION_SECONDS", "37")
    profile = get_model_result_profile()
    assert profile.max_duration_seconds == 37
    assert profile.max_stem_bytes >= 37 * 96000 * 2 * 4
    assert profile.max_total_bytes == 7 * profile.max_stem_bytes
    monkeypatch.setenv("ISOJAM_MAX_AUDIO_DURATION_SECONDS", "38")
    assert get_model_result_profile().id != profile.id
