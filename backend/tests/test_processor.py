import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.processor as processor


@pytest.fixture
def models(tmp_path, monkeypatch):
    entry = SimpleNamespace(
        slug="test-model", checkpoint="model.ckpt", config="model.yaml"
    )
    folder = tmp_path / entry.slug
    folder.mkdir()
    artifacts = {"checkpoint": b"model-weights", "config": b"model: test\n"}
    for kind, data in artifacts.items():
        (folder / getattr(entry, kind)).write_bytes(data)
    package = tmp_path / "package"
    (package / "configs").mkdir(parents=True)
    (package / "configs" / entry.config).write_bytes(artifacts["config"])
    monkeypatch.setattr(processor, "files", lambda _: package)
    metadata = {
        kind: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        for kind, data in artifacts.items()
    }
    monkeypatch.setenv("BS_ROFORMER_MODELS_PATH", str(tmp_path))
    monkeypatch.setitem(sys.modules, "bs_roformer", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules,
        "bs_roformer.model_registry",
        SimpleNamespace(
            DEFAULT_MODEL=entry.slug,
            MODEL_REGISTRY=SimpleNamespace(get=lambda _: entry),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "bs_roformer.checkpoints",
        SimpleNamespace(artifact_metadata=lambda _, kind: metadata[kind]),
    )
    return entry, folder


def test_model_check_verifies_both_pinned_artifacts_without_downloader(models):
    assert processor.verify_models() == models[0].slug


def test_model_check_uses_bundled_config_not_remote_download_metadata(
    models, monkeypatch
):
    metadata = sys.modules["bs_roformer.checkpoints"]
    original = metadata.artifact_metadata

    def remote_metadata(slug, kind):
        if kind == "config":
            return {"size": 4613, "sha256": "0" * 64}
        return original(slug, kind)

    monkeypatch.setattr(metadata, "artifact_metadata", remote_metadata)
    assert processor.verify_models() == models[0].slug


@pytest.mark.parametrize("kind", ["checkpoint", "config"])
@pytest.mark.parametrize("damage", ["missing", "size", "hash"])
def test_model_check_rejects_missing_or_corrupt_cache(models, kind, damage):
    entry, folder = models
    path = folder / getattr(entry, kind)
    if damage == "missing":
        path.unlink()
    elif damage == "size":
        path.write_bytes(b"")
    else:
        path.write_bytes(b"X" * path.stat().st_size)
    with pytest.raises(
        RuntimeError,
        match={"missing": "Missing", "size": "size mismatch", "hash": "hash mismatch"}[
            damage
        ],
    ):
        processor.verify_models()


@pytest.mark.parametrize("value", ["", " ", "relative/models"])
def test_model_cache_requires_absolute_path(monkeypatch, value):
    monkeypatch.setenv("BS_ROFORMER_MODELS_PATH", value)
    with pytest.raises(RuntimeError, match="absolute path"):
        processor.models_directory()


def test_runtime_checks_gpu_and_cache(models, monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(
            __version__="2.13.0+cu130",
            version=SimpleNamespace(cuda="13.0"),
            cuda=SimpleNamespace(
                is_available=lambda: True, get_device_name=lambda _: "Test GPU"
            ),
        ),
    )
    result = processor.check_runtime()
    assert result["status"] == "ready" and result["gpu"] == "Test GPU"
    assert result["model"] == models[0].slug


@pytest.mark.parametrize(
    "cuda, available, message",
    [
        (None, True, "CUDA 13.0"),
        ("12.8", True, "CUDA 13.0"),
        ("13.0", False, "unavailable"),
    ],
)
def test_runtime_rejects_incompatible_or_missing_gpu(
    monkeypatch, cuda, available, message
):
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(
            version=SimpleNamespace(cuda=cuda),
            cuda=SimpleNamespace(is_available=lambda: available),
        ),
    )
    with pytest.raises(RuntimeError, match=message):
        processor.check_runtime()


def test_serving_clears_stale_readiness_and_execs_dispatcher(tmp_path, monkeypatch):
    marker = tmp_path / "ready.json"
    marker.write_text('{"pid":1}')
    monkeypatch.setattr(processor, "READY_FILE", marker)

    def check():
        assert not marker.exists()
        return {"status": "ready"}

    calls = []
    monkeypatch.setattr(processor, "check_runtime", check)
    monkeypatch.setattr(processor.os, "execv", lambda *args: calls.append(args))
    processor.serve()
    assert json.loads(marker.read_text()) == {"pid": processor.os.getpid()}
    assert calls == [
        (
            sys.executable,
            [
                sys.executable,
                "-m",
                "app.dispatcher",
                "--serve",
                "--adapter",
                "local-model",
            ],
        )
    ]


def test_failed_preflight_removes_previous_ready_marker(tmp_path, monkeypatch):
    marker = tmp_path / "ready.json"
    marker.write_text('{"pid":1}')
    monkeypatch.setattr(processor, "READY_FILE", marker)

    def fail():
        raise RuntimeError("No GPU")

    monkeypatch.setattr(processor, "check_runtime", fail)
    with pytest.raises(RuntimeError, match="No GPU"):
        processor.serve()
    assert not marker.exists()


@pytest.mark.parametrize(
    "command, expected",
    [
        (b"python\0-m\0app.dispatcher\0--serve\0", True),
        (b"python\0-m\0app.processor\0serve\0", False),
        (b"python\0-c\0app.dispatcher\0", False),
        (None, False),
    ],
)
def test_health_requires_live_dispatcher_without_importing_torch(
    tmp_path, monkeypatch, command, expected
):
    marker = tmp_path / "ready.json"
    marker.write_text('{"pid":123}')
    monkeypatch.setattr(processor, "READY_FILE", marker)
    monkeypatch.setitem(sys.modules, "torch", None)

    def read_command(path):
        assert path == Path("/proc/123/cmdline")
        if command is None:
            raise FileNotFoundError
        return command

    monkeypatch.setattr(Path, "read_bytes", read_command)
    assert processor.is_ready() is expected


@pytest.mark.parametrize(
    "marker", ["not json", "[]", '{"pid":false}', '{"pid":-1}', "{}"]
)
def test_health_rejects_invalid_readiness_file(tmp_path, monkeypatch, marker):
    path = tmp_path / "ready.json"
    path.write_text(marker)
    monkeypatch.setattr(processor, "READY_FILE", path)
    assert processor.is_ready() is False
