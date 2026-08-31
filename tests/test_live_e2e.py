import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from scripts import live_plugin_e2e
from tests.support.fake_opencode import FakeOpenCodeServer


SCRIPT = Path(__file__).parents[1] / "scripts/live_plugin_e2e.py"


class LiveE2EDriverTest(unittest.TestCase):
    def test_persisted_run_evidence_measures_new_task_directories_and_sessions(self):
        with TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            tasks_root = state_root / "tasks"
            for task_id, session_id in (
                ("oc-existing", "ses-existing"),
                ("oc-new-one", "ses-new-one"),
                ("oc-new-two", "ses-new-two"),
            ):
                task_dir = tasks_root / task_id
                task_dir.mkdir(parents=True)
                (task_dir / "state.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 3,
                            "task_id": task_id,
                            "opencode": {"session_id": session_id},
                        }
                    ),
                    encoding="utf-8",
                )

            collector = getattr(live_plugin_e2e, "_persisted_run_evidence", None)
            self.assertIsNotNone(collector, "live E2E must collect persisted run evidence")
            task_ids, session_ids = collector(state_root, {"oc-existing"})

            self.assertEqual(task_ids, {"oc-new-one", "oc-new-two"})
            self.assertEqual(session_ids, {"ses-new-one", "ses-new-two"})

    def test_dry_run_creates_committed_fixture_without_contacting_server(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--dry-run",
                    "--state-root",
                    str(root),
                    "--server",
                    "http://127.0.0.1:1",
                    "--model",
                    "mcli/glm-5.3",
                    "--effort",
                    "max",
                ],
                text=True,
                capture_output=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            payload = json.loads(completed.stdout)
            source = Path(payload["source_repo"])
            self.assertTrue(payload["dry_run"])
            self.assertTrue((source / "math_utils.py").is_file())
            self.assertTrue((source / "tests/test_math_utils.py").is_file())
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(source), "status", "--porcelain"],
                    check=True,
                    text=True,
                    capture_output=True,
                ).stdout,
                "",
            )
            tests = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "tests",
                    "-t",
                    ".",
                    "-v",
                ],
                cwd=source,
                text=True,
                capture_output=True,
            )
            self.assertEqual(tests.returncode, 0, tests.stderr or tests.stdout)
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(source), "status", "--porcelain"],
                    check=True,
                    text=True,
                    capture_output=True,
                ).stdout,
                "",
            )
            arguments = payload["delegate_arguments"]
            self.assertEqual(
                arguments["task_contract"]["allowed_paths"],
                ["math_utils.py", "tests/test_math_utils.py"],
            )
            self.assertEqual(
                arguments["model"],
                {"providerID": "mcli", "modelID": "glm-5.3"},
            )
            self.assertEqual(arguments["effort"], "max")
            self.assertEqual(arguments["server_url"], "http://127.0.0.1:1")

    def test_full_driver_collects_changes_tests_and_review_ack(self):
        with TemporaryDirectory() as tmp, FakeOpenCodeServer("edit_idle") as server:
            root = Path(tmp) / "state"

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--state-root",
                    str(root),
                    "--server",
                    server.base_url,
                    "--model",
                    "mcli/glm-5.3",
                    "--effort",
                    "max",
                ],
                text=True,
                capture_output=True,
                timeout=20,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            report = json.loads(completed.stdout)
            self.assertTrue(report["ok"])
            self.assertEqual(report["changed_files"], ["math_utils.py", "tests/test_math_utils.py"])
            self.assertEqual(report["session_directory"], report["worktree"])
            self.assertFalse(report["poll_fallback_used"])
            self.assertEqual(
                report["requested_model"],
                {"providerID": "mcli", "modelID": "glm-5.3"},
            )
            self.assertEqual(report["effort"], "max")
            self.assertEqual(report["delegate_call_count"], 1)
            self.assertEqual(report["mcp_call_count"], 5)
            self.assertEqual(report["prompt_count"], 2)
            self.assertEqual(report["task_count"], 1)
            self.assertEqual(report["session_count"], 1)
            self.assertEqual(report["final_review_state"], "AWAITING_INTEGRATION")
            self.assertEqual(
                report["observed_session_model"],
                {"id": "glm-5.3", "providerID": "mcli", "variant": "max"},
            )
            self.assertEqual(report["source_fingerprint_before"], report["source_fingerprint_after"])
            self.assertIn("OPENCODE_REVIEW_ACK", report["review_ack"])
            self.assertTrue(report["transcript"])
            self.assertFalse(report["merged"])
            self.assertFalse(report["pushed"])
            self.assertFalse(report["cleaned"])
            self.assertTrue(Path(report["report_path"]).is_file())

    def test_failed_driver_aborts_only_its_new_task(self):
        with TemporaryDirectory() as tmp, FakeOpenCodeServer(
            "native_external_permission"
        ) as server:
            root = Path(tmp) / "state"
            existing_task = root / "tasks/oc-existing"
            existing_task.mkdir(parents=True)
            (existing_task / "state.json").write_text(
                json.dumps(
                    {
                        "schema_version": 3,
                        "task_id": "oc-existing",
                        "phase": "RUNNING",
                        "execution_state": "RUNNING",
                        "wait_state": "DETACHED",
                        "review_state": "PENDING",
                        "source": {"repo_root": "/managed/user/repo"},
                        "opencode": {
                            "base_url": server.base_url,
                            "directory": "/managed/user/worktree",
                            "session_id": "ses_managed_user",
                            "dispatch_state": "SENT",
                        },
                    }
                ),
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--state-root",
                    str(root),
                    "--server",
                    server.base_url,
                    "--model",
                    "mcli/glm-5.3",
                    "--effort",
                    "max",
                ],
                text=True,
                capture_output=True,
                timeout=20,
            )
            new_tasks = [
                path
                for path in (root / "tasks").iterdir()
                if path.is_dir() and path.name != "oc-existing"
            ]
            existing_state = json.loads((existing_task / "state.json").read_text())

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("delegate outcome", completed.stderr)
            self.assertEqual(len(new_tasks), 1)
            created_state = json.loads((new_tasks[0] / "state.json").read_text())
            self.assertEqual(server.abort_count, 1)
            self.assertNotIn("abort", existing_state)
            self.assertEqual(created_state["execution_state"], "ABORTED")

    def test_dry_run_cross_worktree_read_adds_absolute_external_rule(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            cross_root = root / "fixtures"
            cross_root.mkdir()
            (cross_root / "fixture-a.txt").write_text("a", encoding="utf-8")
            (cross_root / "fixture-b.txt").write_text("b", encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--dry-run",
                    "--state-root",
                    str(state_root),
                    "--server",
                    "http://127.0.0.1:1",
                    "--model",
                    "mcli/glm-5.3",
                    "--effort",
                    "max",
                    "--cross-worktree-read-root",
                    str(cross_root),
                ],
                text=True,
                capture_output=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            payload = json.loads(completed.stdout)
            arguments = payload["delegate_arguments"]
            self.assertEqual(
                arguments["permission_policy"]["rules"],
                [
                    {
                        "permission": "external_directory",
                        "pattern": f"{cross_root.resolve()}/**",
                        "action": "allow",
                    }
                ],
            )
            self.assertIn("fixture-a.txt", arguments["task_contract"]["goal"])
            self.assertIn("fixture-b.txt", arguments["task_contract"]["goal"])

    def test_cross_worktree_read_root_must_be_absolute(self):
        with TemporaryDirectory() as tmp:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--dry-run",
                    "--state-root",
                    str(Path(tmp) / "state"),
                    "--cross-worktree-read-root",
                    "relative-fixtures",
                ],
                text=True,
                capture_output=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("absolute", completed.stderr)


if __name__ == "__main__":
    unittest.main()
