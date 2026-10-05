"""Run locked lint and tests in a disposable copy of the CPU runtime image."""

import argparse
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

LEGACY_MIGRATIONS = "cc4ab351693a_*.py,47d7085ef617_*.py"


def run_inside() -> None:
    if os.getuid() != 0:
        raise RuntimeError(
            "The test container must start as root to install test tools"
        )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--require-hashes",
            "--only-binary=:all:",
            "-r",
            "requirements-dev.txt",
        ],
        check=True,
    )
    subprocess.run([sys.executable, "-m", "pip", "check"], check=True)
    with tempfile.TemporaryDirectory(prefix="isojam-check-") as temporary:
        os.chown(temporary, 10001, 10001)
        os.environ["ISOJAM_DATABASE_PATH"] = str(Path(temporary) / "metadata.db")
        os.environ["ISOJAM_AUDIO_STORAGE_DIR"] = str(Path(temporary) / "audio")
        os.environ.pop("ALEMBIC_DATABASE_URL", None)
        os.setgroups([])
        os.setgid(10001)
        os.setuid(10001)
        print(f"Checks run as UID/GID {os.getuid()}/{os.getgid()}", flush=True)
        paths = ["app", "tests", "scripts", "migrations"]
        for args in (
            ["ruff", "check", "--no-cache", "--extend-exclude", LEGACY_MIGRATIONS],
            [
                "ruff",
                "format",
                "--check",
                "--no-cache",
                "--extend-exclude",
                LEGACY_MIGRATIONS,
            ],
        ):
            subprocess.run([sys.executable, "-m", *args, *paths], check=True)
        subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            check=True,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="isojam-api:local")
    parser.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.inside:
        run_inside()
        return 0
    backend = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            "0:0",
            "--name",
            f"isojam-check-{uuid.uuid4().hex[:12]}",
            "--label",
            "io.isojam.check=true",
            "--mount",
            f"type=bind,src={backend},dst=/workspace/backend,readonly",
            "--workdir",
            "/workspace/backend",
            args.image,
            "python",
            "scripts/check_container.py",
            "--inside",
        ],
        check=False,
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
