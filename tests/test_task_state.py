from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from opencode_orchestrator.task_state import Phase, TaskLockError, TaskStore, new_task_id


class TaskStoreTest(unittest.TestCase):
    def test_create_uses_orthogonal_v3_states_and_progress_defaults(self):
        with TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp))

            state = store.create(
                "oc-20260830-010101-a1b2c3d4",
                "/repo",
                "abc",
                "main",
                "clean",
            )

            self.assertEqual(state["schema_version"], 3)
            self.assertEqual(state["execution_state"], "PREPARING")
            self.assertEqual(state["wait_state"], "DETACHED")
            self.assertEqual(state["review_state"], "PENDING")
            self.assertIn("task_fingerprint", state)
            self.assertIsNone(state["task_fingerprint"])
            self.assertEqual(state["progress"]["last_progress_event"], "task.created")
            self.assertEqual(state["progress"]["heartbeat_count"], 0)
            self.assertEqual(state["permission_audit"], [])
            self.assertEqual(state["task_permission_rules"], [])
            self.assertEqual(state["execution"]["continuation_round"], 0)
            self.assertIsNone(state["execution"]["continuation"])

    def test_update_mutates_under_lock_and_persists_atomically(self):
        with TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp))
            task_id = "oc-20260830-010101-a1b2c3d4"
            original = store.create(task_id, "/repo", "abc", "main", "clean")

            self.assertTrue(hasattr(store, "update"), "TaskStore.update is missing")
            updated = store.update(
                task_id,
                lambda state: state.update(
                    {"wait_state": "ATTACHED", "wait": {"request_id": "request-1"}}
                ),
            )

            self.assertEqual(original["wait_state"], "DETACHED")
            self.assertEqual(updated["wait_state"], "ATTACHED")
            self.assertEqual(store.load(task_id), updated)
            self.assertFalse((store.task_dir(task_id) / "state.json.tmp").exists())

    def test_new_task_id_has_stable_shape(self):
        task_id = new_task_id(now="20260829-173000", entropy="a1b2c3d4")
        self.assertEqual(task_id, "oc-20260829-173000-a1b2c3d4")

    def test_nonblocking_lock_rejects_second_owner(self):
        with TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp))
            task_id = "oc-20260829-173000-a1b2c3d4"
            store.create(task_id, "/repo", "abc", "main", "hash")

            with store.lock(task_id):
                with self.assertRaises(TaskLockError):
                    with store.lock(task_id):
                        self.fail("second task lock unexpectedly acquired")


if __name__ == "__main__":
    unittest.main()
