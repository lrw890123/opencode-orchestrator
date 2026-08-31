import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from opencode_orchestrator.task_state import TaskLock, TaskLockError


TASK_ID = "oc-20260829-010101-a1b2c3d4"


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


class MigrationTest(unittest.TestCase):
    def migration_module(self):
        self.assertIsNotNone(
            importlib.util.find_spec("opencode_orchestrator.migration"),
            "migration module is missing",
        )
        return importlib.import_module("opencode_orchestrator.migration")

    def v1_state(self, phase: str = "PAUSED") -> dict:
        return {
            "schema_version": 1,
            "task_id": TASK_ID,
            "phase": phase,
            "created_at": "2026-08-29T01:01:01+00:00",
            "updated_at": "2026-08-29T01:02:01+00:00",
            "source": {
                "repo_root": "/repo",
                "base_sha": "abc",
                "original_branch": "main",
                "dirty_fingerprint": "clean",
            },
            "worktree": {"path": "/worktree", "branch": "opencode/demo"},
            "opencode": {
                "base_url": "http://127.0.0.1:4096",
                "session_id": "ses_test",
            },
            "policy": {"allowed_paths": ["README.md"]},
            "execution": {"dispatch_marker": "[oc-task:test]"},
        }

    def v2_state(self, phase: str = "RUNNING") -> dict:
        return {
            "schema_version": 2,
            "task_id": TASK_ID,
            "task_fingerprint": "sha256:task",
            "phase": phase,
            "execution_state": "RUNNING",
            "wait_state": "DETACHED",
            "review_state": "PENDING",
            "created_at": "2026-08-29T01:01:01+00:00",
            "updated_at": "2026-08-29T01:02:01+00:00",
            "source": {
                "repo_root": "/repo",
                "base_sha": "abc",
                "original_branch": "main",
                "dirty_fingerprint": "clean",
            },
            "worktree": {"path": "/worktree", "branch": "opencode/demo"},
            "opencode": {
                "base_url": "http://127.0.0.1:4096",
                "directory": "/worktree",
                "session_id": "ses_stuck",
                "dispatch_state": "SENT",
            },
            "policy": {"allowed_paths": ["README.md"]},
            "execution": {
                "dispatch_marker": "[oc-task:test]",
                "event_counts": {"message.part.updated": 2},
            },
            "unknown_field": {"keep": True},
        }

    def test_migrate_paused_v1_task_keeps_execution_alive_but_detached(self):
        migration = self.migration_module()

        migrated = migration.migrate_v1_state(self.v1_state())

        self.assertEqual(migrated["schema_version"], 2)
        self.assertEqual(migrated["execution_state"], "RUNNING")
        self.assertEqual(migrated["wait_state"], "DETACHED")
        self.assertEqual(migrated["review_state"], "PENDING")
        self.assertEqual(migrated["legacy_phase"], "PAUSED")
        self.assertEqual(migrated["opencode"]["session_id"], "ses_test")

    def test_every_v1_phase_has_a_literal_v2_mapping(self):
        migration = self.migration_module()
        expected = {
            "DRAFT": ("PREPARING", "PENDING"),
            "RISK_CHECK": ("PREPARING", "PENDING"),
            "AWAITING_APPROVAL": ("PREPARING", "PENDING"),
            "PREPARING": ("PREPARING", "PENDING"),
            "DISPATCHED": ("PREPARING", "PENDING"),
            "RUNNING": ("RUNNING", "PENDING"),
            "NEEDS_INPUT": ("INPUT_REQUIRED", "PENDING"),
            "PERMISSION_WAIT": ("INPUT_REQUIRED", "PENDING"),
            "PAUSED": ("RUNNING", "PENDING"),
            "COLLECTING": ("COMPLETED", "READY"),
            "REVIEWING": ("COMPLETED", "REVIEWING"),
            "REVISION_REQUESTED": ("RUNNING", "REVISION_REQUESTED"),
            "PASSED": ("COMPLETED", "PASSED"),
            "AWAITING_INTEGRATION": ("COMPLETED", "AWAITING_INTEGRATION"),
            "FAILED": ("FAILED", "PENDING"),
            "CANCELLED": ("ABORTED", "PENDING"),
        }

        observed = {
            phase: (
                migration.EXECUTION_BY_PHASE.get(phase),
                migration.REVIEW_BY_PHASE.get(phase, "PENDING"),
            )
            for phase in expected
        }

        self.assertEqual(observed, expected)

    def test_root_migration_is_copy_based_and_idempotent(self):
        migration = self.migration_module()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy"
            target = root / "target"
            task_dir = legacy / "tasks" / TASK_ID
            task_dir.mkdir(parents=True)
            (task_dir / "state.json").write_text(
                json.dumps(self.v1_state(), ensure_ascii=False), encoding="utf-8"
            )
            (task_dir / "request.json").write_text('{"goal":"demo"}\n', encoding="utf-8")
            (task_dir / "events.jsonl").write_text('{"type":"session.idle"}\n', encoding="utf-8")
            (legacy / "config.json").write_text(
                '{"server_url":"http://127.0.0.1:4096"}\n', encoding="utf-8"
            )
            before = tree_digest(legacy)

            first = migration.migrate_state_root(legacy, target)
            second = migration.migrate_state_root(legacy, target)

            self.assertEqual(first["migrated_tasks"], 1)
            self.assertFalse(first["already_migrated"])
            self.assertTrue(second["already_migrated"])
            self.assertIn("source_digest", first)
            self.assertEqual(first["source_digest"], before)
            self.assertEqual(tree_digest(legacy), before)
            self.assertEqual(
                json.loads((target / "tasks" / TASK_ID / "state.json").read_text())["schema_version"],
                3,
            )
            self.assertEqual(
                (target / "tasks" / TASK_ID / "request.json").read_text(encoding="utf-8"),
                '{"goal":"demo"}\n',
            )
            self.assertTrue((target / "tasks" / TASK_ID / "events.jsonl").is_file())
            self.assertEqual(
                (target / "tasks" / TASK_ID / "events.jsonl").read_text(encoding="utf-8"),
                '{"type":"session.idle"}\n',
            )
            self.assertTrue((target / "config.json").is_file())
            self.assertEqual(
                (target / "config.json").read_text(encoding="utf-8"),
                '{"server_url":"http://127.0.0.1:4096"}\n',
            )
            self.assertTrue((target / "migration.json").is_file())

    def test_root_migration_preserves_non_migration_diagnostic_with_bad_events(self):
        migration = self.migration_module()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy"
            target = root / "target"
            source_task = legacy / "tasks" / TASK_ID
            source_task.mkdir(parents=True)
            source_state = self.v2_state()
            source_state["progress"] = {
                "diagnostic_error": {
                    "kind": "opencode",
                    "message": "preserve-me",
                }
            }
            (source_task / "state.json").write_text(
                json.dumps(source_state), encoding="utf-8"
            )
            (source_task / "events.jsonl").write_text(
                "malformed-with-secret-body\n", encoding="utf-8"
            )

            first = migration.migrate_state_root(legacy, target)
            first_state = json.loads(
                (target / "tasks" / TASK_ID / "state.json").read_text(
                    encoding="utf-8"
                )
            )
            second = migration.migrate_state_root(legacy, target)
            second_state = json.loads(
                (target / "tasks" / TASK_ID / "state.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertFalse(first["already_migrated"])
            self.assertTrue(second["already_migrated"])
            self.assertEqual(first_state, second_state)
            self.assertEqual(
                first_state["progress"]["diagnostic_error"],
                {"kind": "opencode", "message": "preserve-me"},
            )

    def test_migrate_v2_task_record_is_idempotent_and_preserves_identity(self):
        migration = self.migration_module()
        state = self.v2_state()
        request = {
            "goal": "Recover the stuck task",
            "permission_policy": {"default": "allow"},
            "progress_policy": {"stall_timeout_seconds": 900},
        }
        event_records = [
            {
                "recorded_at": "2026-08-29T01:03:00+00:00",
                "event": {"type": "message.part.updated", "properties": {"text": "secret"}},
            },
            {
                "recorded_at": "2026-08-29T01:04:00+00:00",
                "event": {"type": "server.heartbeat", "properties": {"body": "secret"}},
            },
            {
                "recorded_at": "2026-08-29T01:05:00+00:00",
                "event": {"type": "server.heartbeat", "properties": {"body": "secret"}},
            },
        ]

        migrated, diagnostic = migration.migrate_task_record(
            state, request, event_records
        )
        repeated, repeated_diagnostic = migration.migrate_task_record(
            migrated, request, event_records
        )

        self.assertIsNone(diagnostic)
        self.assertIsNone(repeated_diagnostic)
        self.assertEqual(repeated, migrated)
        self.assertEqual(migrated["schema_version"], 3)
        self.assertEqual(migrated["task_id"], state["task_id"])
        self.assertEqual(migrated["opencode"], state["opencode"])
        self.assertEqual(migrated["worktree"], state["worktree"])
        self.assertEqual(
            migrated["execution"]["dispatch_marker"],
            state["execution"]["dispatch_marker"],
        )
        self.assertEqual(migrated["unknown_field"], state["unknown_field"])
        self.assertEqual(
            migrated["permission_policy"],
            {
                "default": "allow",
                "persistence": "task",
                "approval_basis": None,
                "rules": [],
            },
        )
        self.assertEqual(
            migrated["progress_policy"],
            {"input_probe_interval_seconds": 15, "stall_timeout_seconds": 900},
        )
        self.assertEqual(migrated["permission_audit"], [])
        self.assertEqual(migrated["task_permission_rules"], [])
        self.assertEqual(
            migrated["progress"]["last_progress_at"],
            "2026-08-29T01:03:00+00:00",
        )
        self.assertEqual(migrated["progress"]["last_progress_event"], "message.part.updated")
        self.assertEqual(migrated["permission_policy"]["rules"], [])

    def test_migrate_v2_task_uses_updated_at_when_no_nonheartbeat_event_exists(self):
        migration = self.migration_module()
        migrated, diagnostic = migration.migrate_task_record(
            self.v2_state(),
            None,
            [
                {
                    "recorded_at": "2026-08-29T01:03:00+00:00",
                    "event": {"type": "server.heartbeat"},
                }
            ],
        )

        self.assertIsNone(diagnostic)
        self.assertEqual(
            migrated["progress"]["last_progress_at"],
            "2026-08-29T01:02:01+00:00",
        )
        self.assertEqual(migrated["progress"]["last_progress_event"], "task.migrated")

    def test_migrate_task_records_sanitizes_malformed_event_diagnostic_and_is_idempotent(self):
        migration = self.migration_module()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_dir = root / "tasks" / TASK_ID
            task_dir.mkdir(parents=True)
            (task_dir / "state.json").write_text(
                json.dumps(self.v2_state()), encoding="utf-8"
            )
            (task_dir / "request.json").write_text(
                json.dumps({"permission_policy": {"default": "allow"}}),
                encoding="utf-8",
            )
            (task_dir / "events.jsonl").write_text(
                '{"recorded_at":"2026-08-29T01:03:00+00:00","event":{"type":"message.part.updated"}}\n'
                "not-json-with-secret-body\n",
                encoding="utf-8",
            )
            first = migration.migrate_task_records(root)
            migrated = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
            second = migration.migrate_task_records(root)
            repeated = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))

            self.assertEqual(first, {"examined": 1, "migrated": 1, "unchanged": 0})
            self.assertEqual(second, {"examined": 1, "migrated": 0, "unchanged": 1})
            self.assertEqual(repeated, migrated)
            diagnostic = migrated["progress"]["diagnostic_error"]
            self.assertIsInstance(diagnostic, dict)
            self.assertEqual(diagnostic.get("kind"), "migration")
            self.assertNotIn("not-json-with-secret-body", json.dumps(diagnostic))

    def test_combined_policy_and_event_diagnostics_are_stable_on_second_startup(self):
        migration = self.migration_module()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_dir = root / "tasks" / TASK_ID
            task_dir.mkdir(parents=True)
            (task_dir / "state.json").write_text(
                json.dumps(self.v2_state()), encoding="utf-8"
            )
            (task_dir / "request.json").write_text(
                json.dumps(
                    {
                        "permission_policy": {"persistence": "project"},
                        "progress_policy": {"stall_timeout_seconds": 29},
                    }
                ),
                encoding="utf-8",
            )
            (task_dir / "events.jsonl").write_text(
                "{\"recorded_at\":\"2026-08-29T01:03:00+00:00\",\"event\":{\"type\":\"message.part.updated\"}}\n"
                "malformed-with-secret-body\n",
                encoding="utf-8",
            )

            first = migration.migrate_task_records(root)
            first_state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
            second = migration.migrate_task_records(root)
            second_state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))

            self.assertEqual(first, {"examined": 1, "migrated": 1, "unchanged": 0})
            self.assertEqual(second, {"examined": 1, "migrated": 0, "unchanged": 1})
            self.assertEqual(second_state, first_state)
            diagnostic = first_state["progress"]["diagnostic_error"]
            self.assertEqual(diagnostic["kind"], "migration")
            self.assertEqual(diagnostic["code"], "multiple")
            self.assertIn("invalid_permission_policy", diagnostic["codes"])
            self.assertIn("invalid_progress_policy", diagnostic["codes"])
            self.assertIn("malformed_event_json", diagnostic["codes"])
            self.assertNotIn("malformed-with-secret-body", json.dumps(diagnostic))

    def test_corrupt_v2_and_v3_permission_policies_fail_closed_idempotently(self):
        migration = self.migration_module()
        cases = []
        v2 = self.v2_state()
        cases.append(
            (
                "v2-request",
                v2,
                {
                    "permission_policy": {
                        "default": "corrupt-secret",
                        "rules": [{"authorization": "Bearer policy-secret"}],
                    }
                },
            )
        )
        v3, _ = migration.migrate_task_record(self.v2_state(), None)
        v3["permission_policy"] = {
            "default": "allow",
            "persistence": "project",
            "approval_basis": "policy-secret",
            "rules": "corrupt-secret",
        }
        cases.append(("v3-state", v3, None))

        for label, state, request in cases:
            with self.subTest(label=label):
                migrated, diagnostic = migration.migrate_task_record(state, request)
                repeated, repeated_diagnostic = migration.migrate_task_record(
                    migrated, request
                )

                self.assertEqual(
                    migrated["permission_policy"],
                    {
                        "default": "ask",
                        "persistence": "task",
                        "approval_basis": None,
                        "rules": [],
                    },
                )
                self.assertEqual(diagnostic["code"], "invalid_permission_policy")
                self.assertIsNone(repeated_diagnostic)
                self.assertEqual(repeated, migrated)
                self.assertEqual(
                    migrated["progress"]["diagnostic_error"]["code"],
                    "invalid_permission_policy",
                )
                self.assertNotIn("policy-secret", json.dumps(migrated))
                self.assertNotIn("corrupt-secret", json.dumps(migrated))

    def test_v3_missing_state_policy_does_not_recover_allow_from_request(self):
        migration = self.migration_module()
        v3, _ = migration.migrate_task_record(self.v2_state(), None)
        del v3["permission_policy"]
        request = {"permission_policy": {"default": "allow"}}

        migrated, diagnostic = migration.migrate_task_record(v3, request)
        repeated, repeated_diagnostic = migration.migrate_task_record(
            migrated, request
        )

        self.assertEqual(
            migrated["permission_policy"],
            {
                "default": "ask",
                "persistence": "task",
                "approval_basis": None,
                "rules": [],
            },
        )
        self.assertEqual(diagnostic["code"], "invalid_permission_policy")
        self.assertIsNone(repeated_diagnostic)
        self.assertEqual(repeated, migrated)

    def test_connected_events_do_not_advance_progress_baseline(self):
        migration = self.migration_module()
        migrated, diagnostic = migration.migrate_task_record(
            self.v2_state(),
            None,
            [
                {
                    "recorded_at": "2026-08-29T01:05:00+00:00",
                    "event": {"type": "server.connected"},
                }
            ],
        )

        self.assertIsNone(diagnostic)
        self.assertEqual(
            migrated["progress"]["last_progress_at"],
            "2026-08-29T01:02:01+00:00",
        )
        self.assertEqual(migrated["progress"]["last_progress_event"], "task.migrated")

    def test_malformed_valid_event_records_are_diagnosed_without_exposing_bodies(self):
        migration = self.migration_module()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_dir = root / "tasks" / TASK_ID
            task_dir.mkdir(parents=True)
            (task_dir / "state.json").write_text(
                json.dumps(self.v2_state()), encoding="utf-8"
            )
            (task_dir / "events.jsonl").write_text(
                "{\"recorded_at\":\"not-a-timestamp\",\"event\":{\"type\":\"message.part.updated\",\"body\":\"secret-one\"}}\n"
                "{\"recorded_at\":\"2026-08-29T01:04:00+00:00\",\"event\":{\"type\":123,\"body\":\"secret-two\"}}\n",
                encoding="utf-8",
            )

            counts = migration.migrate_task_records(root)
            state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
            diagnostic = state["progress"]["diagnostic_error"]

            self.assertEqual(counts, {"examined": 1, "migrated": 1, "unchanged": 0})
            self.assertEqual(diagnostic["kind"], "migration")
            self.assertTrue(diagnostic.get("code"))
            self.assertEqual(diagnostic.get("lines"), [1, 2])
            self.assertNotIn("secret-one", json.dumps(diagnostic))
            self.assertNotIn("secret-two", json.dumps(diagnostic))
            self.assertEqual(
                state["progress"]["last_progress_at"],
                self.v2_state()["updated_at"],
            )

    def test_invalid_utf8_event_log_is_diagnosed_and_uses_updated_at(self):
        migration = self.migration_module()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_dir = root / "tasks" / TASK_ID
            task_dir.mkdir(parents=True)
            (task_dir / "state.json").write_text(
                json.dumps(self.v2_state()), encoding="utf-8"
            )
            (task_dir / "events.jsonl").write_bytes(b"{\"type\":\"session.idle\"}\n\xff\n")

            counts = migration.migrate_task_records(root)
            state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
            diagnostic = state["progress"]["diagnostic_error"]

            self.assertEqual(counts, {"examined": 1, "migrated": 1, "unchanged": 0})
            self.assertEqual(diagnostic["kind"], "migration")
            self.assertEqual(diagnostic["code"], "invalid_event_encoding")
            self.assertEqual(
                state["progress"]["last_progress_at"],
                self.v2_state()["updated_at"],
            )

    def test_non_migration_diagnostic_survives_malformed_events_across_startup(self):
        migration = self.migration_module()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_dir = root / "tasks" / TASK_ID
            task_dir.mkdir(parents=True)
            state, diagnostic = migration.migrate_task_record(self.v2_state(), None)
            self.assertIsNone(diagnostic)
            state["progress"]["diagnostic_error"] = {
                "kind": "opencode",
                "message": "preserve-me",
            }
            (task_dir / "state.json").write_text(
                json.dumps(state), encoding="utf-8"
            )
            (task_dir / "events.jsonl").write_text(
                "malformed-with-secret-body\n", encoding="utf-8"
            )

            first = migration.migrate_task_records(root)
            first_state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
            second = migration.migrate_task_records(root)
            second_state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))

            self.assertEqual(first, {"examined": 1, "migrated": 0, "unchanged": 1})
            self.assertEqual(second, {"examined": 1, "migrated": 0, "unchanged": 1})
            self.assertEqual(second_state, first_state)
            self.assertEqual(
                first_state["progress"]["diagnostic_error"],
                {"kind": "opencode", "message": "preserve-me"},
            )

    def test_state_roots_honor_explicit_override_and_codex_home(self):
        migration = self.migration_module()
        with TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex"

            target, legacy = migration.resolve_state_roots({"CODEX_HOME": str(codex_home)})
            explicit, explicit_legacy = migration.resolve_state_roots(
                {
                    "CODEX_HOME": str(codex_home),
                    "OPENCODE_ORCHESTRATOR_STATE_ROOT": str(Path(tmp) / "explicit"),
                }
            )

            self.assertEqual(target, (codex_home / "plugin-data/opencode-orchestrator").resolve())
            self.assertEqual(legacy, (codex_home / "opencode-orchestrator").resolve())
            self.assertEqual(explicit, (Path(tmp) / "explicit").resolve())
            self.assertEqual(explicit_legacy, legacy)

    def test_retry_after_task_copy_but_before_marker_finishes_migration(self):
        migration = self.migration_module()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy"
            target = root / "target"
            source_task = legacy / "tasks" / TASK_ID
            source_task.mkdir(parents=True)
            (source_task / "state.json").write_text(
                json.dumps(self.v1_state("RUNNING")), encoding="utf-8"
            )
            (source_task / "request.json").write_text('{"goal":"demo"}\n', encoding="utf-8")
            migration.migrate_state_root(legacy, target)
            (target / "migration.json").unlink()

            try:
                recovered = migration.migrate_state_root(legacy, target)
            except FileExistsError as error:
                self.fail(f"migration cannot recover an already copied task: {error}")

            self.assertEqual(recovered["migrated_tasks"], 1)
            self.assertTrue((target / "migration.json").is_file())

    def test_second_migration_process_is_rejected_by_root_lock(self):
        migration = self.migration_module()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy"
            target = root / "target"
            legacy.mkdir()

            with TaskLock(target / "migration.lock"):
                with self.assertRaises(TaskLockError):
                    migration.migrate_state_root(legacy, target)


if __name__ == "__main__":
    unittest.main()
