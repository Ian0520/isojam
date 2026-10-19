"""Regenerate Linux/Python 3.12 CPU locks or optional runtime additions."""

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
    additions = parser.add_mutually_exclusive_group()
    additions.add_argument(
        "--gpu",
        action="store_true",
        help="Generate GPU additions constrained by the API lock.",
    )
    additions.add_argument(
        "--youtube",
        action="store_true",
        help="Generate YouTube importer additions constrained by the API lock.",
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
    if args.gpu:
        command += " --gpu"
    elif args.youtube:
        command += " --youtube"
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
        "--allow-unsafe",
        "--strip-extras",
        "--no-emit-options",
    ]
    if args.upgrade:
        common.append("--upgrade")
    if args.gpu or args.youtube:
        names = (
            ("requirements-gpu", "requirements-gpu-build")
            if args.gpu
            else ("requirements-youtube",)
        )
        for name in names:
            subprocess.run(
                [*common, "--output-file", name + ".txt", name + ".in"],
                cwd=backend,
                env=env,
                check=True,
            )
            print(f"Generated {name}.txt", flush=True)
        return
    common.append("--all-build-deps")
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
