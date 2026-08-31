import json
import importlib
import importlib.util
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from tests.test_git_workspace import create_repo
from tests.test_service_integration_unit import LOW_REQUEST
from opencode_orchestrator.task_state import Phase, TaskStore


CLI = Path(__file__).parents[1] / "scripts/oc_bridge.py"


class CliTest(unittest.TestCase):
    def test_source_package_exposes_release_version(self):
        source_root = Path(__file__).parents[1] / "src"
        sys.path.insert(0, str(source_root))
        try:
            self.assertIsNotNone(importlib.util.find_spec("opencode_orchestrator"))
            module = importlib.import_module("opencode_orchestrator")
        finally:
            sys.path.remove(str(source_root))

        self.assertEqual(module.__version__, "2.1.4")

    def test_version_prints_one_json_object(self):
        completed = subprocess.run(
            [sys.executable, str(CLI), "version"],
            text=True,
            capture_output=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(
            json.loads(completed.stdout),
            {"ok": True, "schema_version": 1, "version": "0.1.0"},
        )
        self.assertEqual(completed.stdout.count("\n"), 1)

    def test_missing_command_returns_json_usage_error(self):
        completed = subprocess.run(
            [sys.executable, str(CLI)],
            text=True,
            capture_output=True,
        )

        self.assertEqual(completed.returncode, 2)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "usage")

    def test_prepare_command_returns_json_state(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = create_repo(root / "source")
            request_path = root / "request.json"
            request_path.write_text(json.dumps(LOW_REQUEST), encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "prepare",
                    "--state-root",
                    str(root / "state"),
                    "--repo",
                    str(source),
                    "--slug",
                    "cli-demo",
                    "--request",
                    str(request_path),
                    "--server",
                    "http://127.0.0.1:4096",
                ],
                text=True,
                capture_output=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["phase"], "PREPARING")
            self.assertTrue(Path(payload["worktree"]["path"]).is_dir())

    def test_approve_review_command_records_evidence_without_integrating(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = TaskStore(root / "state")
            task_id = "oc-20260829-173000-a1b2c3d4"
            store.create(task_id, "/repo", "abc", "main", "clean")
            for phase in (
                Phase.RISK_CHECK,
                Phase.PREPARING,
                Phase.DISPATCHED,
                Phase.RUNNING,
                Phase.COLLECTING,
                Phase.REVIEWING,
            ):
                store.transition(task_id, phase)
            evidence_path = root / "review.json"
            evidence_path.write_text(
                json.dumps(
                    {
                        "tests_passed": True,
                        "review_summary": "Codex reviewed every changed file and reran tests.",
                    }
                ),
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "approve-review",
                    "--state-root",
                    str(root / "state"),
                    "--task-id",
                    task_id,
                    "--payload",
                    str(evidence_path),
                ],
                text=True,
                capture_output=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["phase"], Phase.AWAITING_INTEGRATION)
            self.assertEqual(payload["review"]["tests_passed"], True)


if __name__ == "__main__":
    unittest.main()
