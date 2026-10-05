import hashlib
import json
import multiprocessing
import os
import time
from io import BytesIO
from uuid import uuid4

import pytest

import app.results as results
from app.audio import validate_wav
from app.fake_worker import create_fake_workspace
from app.results import (
    FAKE_RESULT_PROFILE,
    MAX_MANIFEST_BYTES,
    LocalResultStore,
    ResultConflictError,
    ResultIdentity,
    ResultValidationError,
)
from tests.factories import make_wav_bytes


@pytest.fixture
def bundle_inputs(tmp_path):
    store = LocalResultStore(tmp_path / "durable" / "results")
    identity = ResultIdentity(job_id=uuid4(), attempt_id=uuid4(), invocation_id=uuid4())
    workspace = tmp_path / "private"
    workspace.mkdir()
    create_fake_workspace(workspace)
    return store, identity, workspace


def finalized_snapshot(store, identity):
    directory = (store.root / store.manifest_key(identity)).parent
    return {
        path.name: (path.stat().st_ino, path.read_bytes())
        for path in directory.iterdir()
        if not path.name.startswith(".")
    }


def rewrite_manifest(store, identity, body):
    path = store.root / store.manifest_key(identity)
    path.chmod(0o600)
    path.write_text(json.dumps(body))


def test_complete_bundle_is_verified_and_identical_retry_preserves_final_files(
    bundle_inputs,
):
    store, identity, workspace = bundle_inputs
    bundle = store.write_bundle(identity, workspace)
    assert bundle.manifest.profile_id == FAKE_RESULT_PROFILE.id
    assert len(bundle.outputs) == 7
    for artifact in bundle.outputs:
        raw = artifact.path.read_bytes()
        assert artifact.byte_count == len(raw)
        assert artifact.sha256 == hashlib.sha256(raw).hexdigest()
        assert artifact.path.stat().st_mode & 0o777 == 0o400
    before = finalized_snapshot(store, identity)
    assert store.write_bundle(identity, workspace) == bundle
    assert finalized_snapshot(store, identity) == before
    assert not list((store.root / bundle.manifest_key).parent.glob(".*.tmp"))
    other = identity.model_copy(update={"invocation_id": uuid4()})
    second = store.write_bundle(other, workspace)
    assert second.manifest_key != bundle.manifest_key
    assert finalized_snapshot(store, identity) == before


def test_conflicting_retry_never_overwrites_finalized_artifact(bundle_inputs):
    store, identity, workspace = bundle_inputs
    store.write_bundle(identity, workspace)
    before = finalized_snapshot(store, identity)
    (workspace / "vocals.wav").write_bytes(make_wav_bytes())
    with pytest.raises(ResultConflictError, match="different finalized WAV"):
        store.write_bundle(identity, workspace)
    assert finalized_snapshot(store, identity) == before


def test_conflicting_manifest_never_replaces_existing_manifest(bundle_inputs):
    store, identity, workspace = bundle_inputs
    store.write_bundle(identity, workspace)
    manifest_path = store.root / store.manifest_key(identity)
    body = json.loads(manifest_path.read_bytes())
    body["profile_id"] = "conflicting-profile"
    rewrite_manifest(store, identity, body)
    before = finalized_snapshot(store, identity)
    with pytest.raises(ResultConflictError, match="different finalized manifest"):
        store.write_bundle(identity, workspace)
    assert finalized_snapshot(store, identity) == before


def killed_writer(root, identity_data, workspace, signal):
    store = LocalResultStore(root)
    install = store._install

    def stall_after_first_file(directory, temporary, name):
        installed = install(directory, temporary, name)
        signal.send(name)
        time.sleep(30)
        return installed

    store._install = stall_after_first_file
    store.write_bundle(ResultIdentity.model_validate(identity_data), workspace)


def test_killed_writer_leaves_no_ready_manifest_and_identical_retry_preserves_file(
    bundle_inputs,
):
    store, identity, workspace = bundle_inputs
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=killed_writer,
        args=(store.root, identity.model_dump(), workspace, sender),
    )
    try:
        process.start()
        sender.close()
        assert receiver.poll(15), "writer did not reach the first install boundary"
        first_name = receiver.recv()
        process.terminate()
        process.join(timeout=5)
        assert not process.is_alive() and process.exitcode != 0
        manifest = store.root / store.manifest_key(identity)
        assert not manifest.exists()
        first = manifest.parent / first_name
        before = (first.stat().st_ino, first.read_bytes())
        with pytest.raises(ResultValidationError):
            LocalResultStore(store.root).verify_bundle(identity)
        # This tests storage replay only. A repeated execution grant stays denied.
        store.write_bundle(identity, workspace)
        assert (first.stat().st_ino, first.read_bytes()) == before
        assert len(store.verify_bundle(identity).outputs) == 7
    finally:
        if process.is_alive():
            process.terminate()
        if process.pid is not None:
            process.join(timeout=5)
        receiver.close()
        sender.close()


def test_manifest_is_installed_only_after_all_stems(bundle_inputs, monkeypatch):
    store, identity, workspace = bundle_inputs
    install = store._install
    order = []

    def observe(directory, temporary, name):
        order.append(name)
        if name == "manifest.json":
            with pytest.raises(ResultValidationError):
                store.verify_bundle(identity)
            for stem in FAKE_RESULT_PROFILE.required_stems:
                assert (store.root / store.artifact_key(identity, stem)).is_file()
            raise OSError("disk failed before manifest install")
        return install(directory, temporary, name)

    monkeypatch.setattr(store, "_install", observe)
    with pytest.raises(ResultValidationError, match="safely written"):
        store.write_bundle(identity, workspace)
    assert order == [
        *(f"{stem}.wav" for stem in FAKE_RESULT_PROFILE.required_stems),
        "manifest.json",
    ]
    assert not (store.root / store.manifest_key(identity)).exists()
    monkeypatch.setattr(store, "_install", install)
    before = finalized_snapshot(store, identity)
    store.write_bundle(identity, workspace)
    after = finalized_snapshot(store, identity)
    assert {name: after[name] for name in before} == before


@pytest.mark.parametrize(
    "corruption",
    [
        "missing",
        "duplicate",
        "unknown",
        "identity",
        "profile",
        "protocol",
        "size",
        "hash",
        "escape",
        "absolute",
        "other_invocation",
        "extra_field",
        "boolean_size",
    ],
)
def test_manifest_cannot_redefine_expected_result(bundle_inputs, corruption):
    store, identity, workspace = bundle_inputs
    bundle = store.write_bundle(identity, workspace)
    body = bundle.manifest.model_dump(mode="json")
    if corruption == "missing":
        body["outputs"].pop()
    elif corruption == "duplicate":
        body["outputs"][-1] = body["outputs"][0]
    elif corruption == "unknown":
        body["outputs"][0]["stem"] = "unexpected"
    elif corruption == "identity":
        body["identity"]["job_id"] = str(uuid4())
    elif corruption == "profile":
        body["profile_id"] = "real-model"
    elif corruption == "protocol":
        body["protocol_version"] = 2
    elif corruption == "size":
        body["outputs"][0]["byte_count"] += 1
    elif corruption == "hash":
        body["outputs"][0]["sha256"] = "0" * 64
    elif corruption == "escape":
        body["outputs"][0]["key"] = "../../outside.wav"
    elif corruption == "absolute":
        body["outputs"][0]["key"] = "/tmp/outside.wav"
    elif corruption == "other_invocation":
        other = identity.model_copy(update={"invocation_id": uuid4()})
        body["outputs"][0]["key"] = store.artifact_key(other, "vocals")
    elif corruption == "extra_field":
        body["untrusted"] = "value"
    else:
        body["outputs"][0]["byte_count"] = True
    rewrite_manifest(store, identity, body)
    with pytest.raises(ResultValidationError):
        store.verify_bundle(identity)


@pytest.mark.parametrize(
    "corruption",
    ["missing", "changed", "invalid_audio", "symlink", "fifo", "directory"],
)
def test_verification_rejects_missing_changed_or_unsafe_artifact(
    bundle_inputs, corruption
):
    store, identity, workspace = bundle_inputs
    bundle = store.write_bundle(identity, workspace)
    path = bundle.outputs[0].path
    path.unlink()
    if corruption == "changed":
        path.write_bytes(make_wav_bytes())
    elif corruption == "invalid_audio":
        raw = b"not a WAV"
        path.write_bytes(raw)
        body = bundle.manifest.model_dump(mode="json")
        body["outputs"][0].update(
            byte_count=len(raw), sha256=hashlib.sha256(raw).hexdigest()
        )
        rewrite_manifest(store, identity, body)
    elif corruption == "symlink":
        path.symlink_to(workspace / "vocals.wav")
    elif corruption == "fifo":
        os.mkfifo(path)
    elif corruption == "directory":
        path.mkdir()
    with pytest.raises(ResultValidationError):
        store.verify_bundle(identity)


@pytest.mark.parametrize("location", ["namespace", "manifest", "workspace"])
def test_symlinks_cannot_redirect_bundle_access(bundle_inputs, location, tmp_path):
    store, identity, workspace = bundle_inputs
    store.write_bundle(identity, workspace)
    if location == "namespace":
        jobs = store.root / "jobs"
        moved = tmp_path / "moved"
        jobs.rename(moved)
        jobs.symlink_to(moved, target_is_directory=True)
    elif location == "manifest":
        path = store.root / store.manifest_key(identity)
        moved = tmp_path / "manifest.json"
        path.rename(moved)
        path.symlink_to(moved)
    else:
        link = tmp_path / "workspace-link"
        link.symlink_to(workspace, target_is_directory=True)
        with pytest.raises(ResultValidationError):
            store.write_bundle(identity, link)
        return
    with pytest.raises(ResultValidationError):
        store.verify_bundle(identity)
    with pytest.raises(ResultValidationError):
        store.write_bundle(identity, workspace)


@pytest.mark.parametrize("kind", ["manifest", "stem", "total", "duration"])
def test_result_limits_are_enforced_when_reading_and_writing(bundle_inputs, kind):
    store, identity, workspace = bundle_inputs
    bundle = store.write_bundle(identity, workspace)
    profile = FAKE_RESULT_PROFILE
    if kind == "manifest":
        path = store.root / bundle.manifest_key
        path.chmod(0o600)
        path.write_bytes(b" " * (MAX_MANIFEST_BYTES + 1))
        with pytest.raises(ResultValidationError):
            store.verify_bundle(identity)
        return
    if kind == "stem":
        profile = profile.model_copy(update={"max_stem_bytes": 100})
    elif kind == "total":
        profile = profile.model_copy(update={"max_total_bytes": 200})
    else:
        raw = make_wav_bytes(frames=8001, sample_rate=8000)
        path = bundle.outputs[0].path
        path.chmod(0o600)
        path.write_bytes(raw)
        (workspace / "vocals.wav").write_bytes(raw)
        body = bundle.manifest.model_dump(mode="json")
        body["outputs"][0].update(
            byte_count=len(raw), sha256=hashlib.sha256(raw).hexdigest()
        )
        rewrite_manifest(store, identity, body)
    with pytest.raises(ResultValidationError):
        store.verify_bundle(identity, profile=profile)
    other = identity.model_copy(update={"invocation_id": uuid4()})
    with pytest.raises(ResultValidationError):
        store.write_bundle(other, workspace, profile=profile)
    assert not (store.root / store.manifest_key(other)).exists()


@pytest.mark.parametrize("change", ["missing", "extra", "symlink", "bad_audio"])
def test_workspace_requires_complete_valid_stems(bundle_inputs, change):
    store, identity, workspace = bundle_inputs
    path = workspace / "vocals.wav"
    if change == "missing":
        path.unlink()
    elif change == "extra":
        (workspace / "extra.wav").write_bytes(make_wav_bytes())
    elif change == "symlink":
        path.unlink()
        path.symlink_to(workspace / "drums.wav")
    else:
        path.write_bytes(b"invalid")
    with pytest.raises(ResultValidationError):
        store.write_bundle(identity, workspace)
    assert not (store.root / store.manifest_key(identity)).exists()


def test_change_during_wav_validation_is_rejected(bundle_inputs, monkeypatch):
    store, identity, workspace = bundle_inputs
    bundle = store.write_bundle(identity, workspace)
    validate = results.validate_wav

    def change_after_decode(file, **kwargs):
        validate(file, **kwargs)
        path = bundle.outputs[0].path
        path.chmod(0o600)
        path.write_bytes(make_wav_bytes())

    monkeypatch.setattr(results, "validate_wav", change_after_decode)
    with pytest.raises(ResultValidationError, match="changed during verification"):
        store.verify_bundle(identity)


def test_wav_validation_supports_open_stream_without_closing_it():
    with BytesIO(make_wav_bytes()) as file:
        validate_wav(file, max_duration_seconds=1)
        assert not file.closed
