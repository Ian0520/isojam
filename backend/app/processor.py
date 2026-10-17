"""Prepare/check the GPU runtime, then exec the existing dispatcher."""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

READY_FILE = Path("/tmp/isojam-processor-ready.json")
DEFAULT_MODELS_DIR = Path("/var/cache/isojam/models")


def models_directory() -> Path:
    value = os.environ.get("BS_ROFORMER_MODELS_PATH", str(DEFAULT_MODELS_DIR))
    if not value.strip() or not Path(value).is_absolute():
        raise RuntimeError("BS_ROFORMER_MODELS_PATH must be a nonblank absolute path")
    return Path(value).resolve()


def verify_models() -> str:
    """Check both pinned artifacts without invoking the downloader."""
    from bs_roformer.checkpoints import artifact_metadata
    from bs_roformer.model_registry import DEFAULT_MODEL, MODEL_REGISTRY

    entry = MODEL_REGISTRY.get(DEFAULT_MODEL)
    directory = models_directory() / entry.slug
    for kind, name in (("checkpoint", entry.checkpoint), ("config", entry.config)):
        path = directory / name
        metadata = artifact_metadata(entry.slug, kind)
        if not path.is_file():
            raise RuntimeError(
                f"Missing model artifact: {path}. Run the prepare-models command first."
            )
        if path.stat().st_size != metadata["size"]:
            raise RuntimeError(f"Model artifact size mismatch: {path}")
        with path.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if digest != metadata["sha256"]:
            raise RuntimeError(f"Model artifact hash mismatch: {path}")
    return entry.slug


def check_runtime() -> dict:
    import torch

    if torch.version.cuda != "13.0":
        raise RuntimeError(
            f"Expected the CUDA 13.0 PyTorch build; got {torch.version.cuda}"
        )
    if not torch.cuda.is_available():
        raise RuntimeError(
            "CUDA GPU is unavailable. Enable Docker GPU access and check the host driver."
        )
    model = verify_models()
    return {
        "status": "ready",
        "torch": str(torch.__version__),
        "cuda": torch.version.cuda,
        "gpu": torch.cuda.get_device_name(0),
        "model": model,
        "models_dir": str(models_directory()),
    }


def prepare_models() -> None:
    from bs_roformer.download import ensure_model_assets
    from bs_roformer.model_registry import DEFAULT_MODEL

    ensure_model_assets(DEFAULT_MODEL, models_dir=models_directory())
    verify_models()
    print("Model cache verified", flush=True)


def is_ready() -> bool:
    """Cheap container health check; never import Torch or load the model."""
    try:
        marker = json.loads(READY_FILE.read_text())
        pid = marker["pid"]
        if type(pid) is not int or pid <= 0:
            return False
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        return command[1:3] == [b"-m", b"app.dispatcher"]
    except (OSError, ValueError, KeyError, TypeError):
        return False


def serve() -> None:
    READY_FILE.unlink(missing_ok=True)
    print(json.dumps(check_runtime()), flush=True)
    # exec retains this PID and removes the preflight's Torch/CUDA state. The
    # dispatcher remains the direct supervised child and receives stop signals.
    temporary = READY_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps({"pid": os.getpid()}))
    temporary.replace(READY_FILE)
    os.execv(
        sys.executable,
        [sys.executable, "-m", "app.dispatcher", "--serve", "--adapter", "local-model"],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["prepare-models", "check", "health", "serve"]
    )
    args = parser.parse_args()
    try:
        if args.command == "health":
            return 0 if is_ready() else 1
        if args.command == "prepare-models":
            prepare_models()
        elif args.command == "check":
            print(json.dumps(check_runtime()), flush=True)
        else:
            serve()
    except (OSError, RuntimeError, ImportError, ValueError) as error:
        print(f"Processor startup failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
