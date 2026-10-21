"""Exercise the host wrapper without starting containers or touching AWS."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
WRAPPER = REPO / "deploy/aws/stack.sh"


class StackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.settings = self.directory / "runtime settings.env"
        self.settings.write_text("ISOJAM_JWT_SECRET_KEY=test-only-secret\n")
        self.log = self.directory / "calls.json"
        docker = self.directory / "docker"
        docker.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "Path(os.environ['DOCKER_CALL_LOG']).write_text(json.dumps({\n"
            " 'args': sys.argv[1:], 'cwd': os.getcwd(),\n"
            " 'jwt': os.environ.get('ISOJAM_JWT_SECRET_KEY')}))\n"
            "sys.exit(int(os.environ.get('DOCKER_EXIT_CODE', '0')))\n"
        )
        docker.chmod(0o755)
        self.env = os.environ.copy()
        self.env.pop("ISOJAM_JWT_SECRET_KEY", None)
        self.env.update(
            PATH=f"{self.directory}{os.pathsep}{self.env['PATH']}",
            DOCKER_CALL_LOG=str(self.log),
            ISOJAM_ENV_FILE=str(self.settings),
        )

    def run_wrapper(self, *args):
        return subprocess.run(
            ["bash", str(WRAPPER), *args],
            cwd=self.directory,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def call(self):
        return json.loads(self.log.read_text())

    def test_start_finds_checkout_and_accepts_env_path_with_spaces(self):
        response = self.run_wrapper("start")
        self.assertEqual(response.returncode, 0, response.stderr)
        call = self.call()
        self.assertEqual(call["cwd"], str(REPO))
        args = call["args"]
        self.assertEqual(args[args.index("--env-file") + 1], str(self.settings))
        self.assertEqual(args[args.index("--project-name") + 1], "isojam-aws")
        self.assertEqual(
            args[-6:],
            ["up", "--detach", "--wait", "--wait-timeout", "180", "--no-build"],
        )

    def test_start_without_settings_fails_before_calling_docker(self):
        self.settings.unlink()
        response = self.run_wrapper("start")
        self.assertNotEqual(response.returncode, 0)
        self.assertIn("Missing readable runtime settings", response.stderr)
        self.assertFalse(self.log.exists())

    def test_shutdown_survives_lost_settings_and_keeps_service_grace_periods(self):
        self.settings.unlink()
        response = self.run_wrapper("stop")
        self.assertEqual(response.returncode, 0, response.stderr)
        call = self.call()
        args = call["args"]
        self.assertEqual(args[args.index("--env-file") + 1], "/dev/null")
        self.assertEqual(args[-1], "stop")
        self.assertNotIn("--timeout", args)
        self.assertNotIn("-t", args)
        self.assertNotIn("down", args)
        self.assertEqual(call["jwt"], "unused-during-stop")

    def test_failed_docker_start_is_reported_to_systemd(self):
        self.env["DOCKER_EXIT_CODE"] = "17"
        response = self.run_wrapper("start")
        self.assertEqual(response.returncode, 17)

    def test_invalid_command_cannot_start_containers(self):
        response = self.run_wrapper("up")
        self.assertEqual(response.returncode, 2)
        self.assertIn("Usage:", response.stderr)
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
