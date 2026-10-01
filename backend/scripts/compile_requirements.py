"""Regenerate the Linux/Python 3.12 API and development dependency locks."""

import argparse
import os
import platform
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--upgrade", action="store_true", help="Resolve newer compatible versions."
    )
    args = parser.parse_args()
    if (
        platform.python_implementation() != "CPython"
        or sys.version_info[:2] != (3, 12)
        or sys.platform != "linux"
        or platform.machine() != "x86_64"
    ):
        parser.error("Run with CPython 3.12 on Linux x86_64.")
    for package, expected in {"pip": "26.2.1", "pip-tools": "7.6.1"}.items():
        try:
            installed = version(package)
        except PackageNotFoundError:
            installed = None
        if installed != expected:
            parser.error(f"Install {package}=={expected} in the tooling environment.")

    backend = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    command = "python scripts/compile_requirements.py"
    if args.upgrade:
        command += " --upgrade"
    env["CUSTOM_COMPILE_COMMAND"] = command
    common = [
        sys.executable,
        "-m",
        "piptools",
        "compile",
        "--quiet",
        "--generate-hashes",
        "--all-build-deps",
        "--allow-unsafe",
        "--strip-extras",
        "--no-emit-options",
    ]
    if args.upgrade:
        common.append("--upgrade")
    for output, extra in [
        ("requirements-api.txt", []),
        (
            "requirements-dev.txt",
            ["--extra", "dev", "--constraint", "requirements-api.txt"],
        ),
    ]:
        subprocess.run(
            [*common, *extra, "--output-file", output, "pyproject.toml"],
            cwd=backend,
            env=env,
            check=True,
        )
        print(f"Generated {output}", flush=True)


if __name__ == "__main__":
    main()
