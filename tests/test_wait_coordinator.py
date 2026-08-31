import importlib
import importlib.util
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from opencode_orchestrator.task_state import TaskLock, TaskLockError, TaskStore


TASK_ID = "oc-20260830-010101-a1b2c3d4"


class WaitCoordinatorTest(unittest.TestCase):
    def coordinator_class(self):
        self.assertIsNotNone(
            importlib.util.find_spec("opencode_orchestrator.wait_coordinator"),
            "wait coordinator module is missing",
        )
        return importlib.import_module(
            "opencode_orchestrator.wait_coordinator"
        ).WaitCoordinator

    def make_store(self, root: Path) -> TaskStore:
        store = TaskStore(root)
        store.create(TASK_ID, "/repo", "abc", "main", "clean")
        return store

    def test_only_one_waiter_can_attach_to_a_task(self):
        coordinator_class = self.coordinator_class()
        with TemporaryDirectory() as tmp:
            store = self.make_store(Path(tmp))
            coordinator = coordinator_class(store)

            with coordinator.attach(TASK_ID, "request-1") as first:
                state = store.load(TASK_ID)
                self.assertEqual(first.task_id, TASK_ID)
                self.assertEqual(first.request_id, "request-1")
                self.assertEqual(state["wait_state"], "ATTACHED")
                self.assertEqual(state["wait"]["owner_pid"], os.getpid())
                with self.assertRaisesRegex(TaskLockError, "waiter"):
                    with coordinator.attach(TASK_ID, "request-2"):
                        self.fail("second waiter unexpectedly attached")

            self.assertEqual(store.load(TASK_ID)["wait_state"], "DETACHED")

    def test_cancel_task_signals_active_lease_and_persists_reason(self):
        coordinator_class = self.coordinator_class()
        with TemporaryDirectory() as tmp:
            store = self.make_store(Path(tmp))
            coordinator = coordinator_class(store)

            with coordinator.attach(TASK_ID, "request-1") as lease:
                self.assertTrue(coordinator.cancel_task(TASK_ID, "external"))
                self.assertTrue(lease.token.cancelled)
                self.assertEqual(lease.token.reason, "external")

            state = store.load(TASK_ID)
            self.assertEqual(state["wait_state"], "CANCELLED")
            self.assertEqual(state["wait"]["disconnect_reason"], "external")
            self.assertFalse(coordinator.cancel_task(TASK_ID, "again"))

    def test_cancel_request_targets_the_matching_lease(self):
        coordinator_class = self.coordinator_class()
        with TemporaryDirectory() as tmp:
            store = self.make_store(Path(tmp))
            coordinator = coordinator_class(store)

            with coordinator.attach(TASK_ID, "request-1") as lease:
                self.assertFalse(coordinator.cancel_request("request-other", "client"))
                self.assertTrue(coordinator.cancel_request("request-1", "client"))
                self.assertTrue(lease.token.cancelled)

    def test_file_lock_rejects_a_second_coordinator(self):
        coordinator_class = self.coordinator_class()
        with TemporaryDirectory() as tmp:
            store = self.make_store(Path(tmp))
            first = coordinator_class(store)
            second = coordinator_class(store)

            with first.attach(TASK_ID, "request-1"):
                with self.assertRaisesRegex(TaskLockError, "waiter"):
                    with second.attach(TASK_ID, "request-2"):
                        self.fail("cross-coordinator waiter unexpectedly attached")

    def test_startup_repairs_attached_state_owned_by_a_dead_process(self):
        coordinator_class = self.coordinator_class()
        with TemporaryDirectory() as tmp:
            store = self.make_store(Path(tmp))
            store.update(
                TASK_ID,
                lambda state: state.update(
                    {
                        "wait_state": "ATTACHED",
                        "wait": {
                            "owner_pid": 424242,
                            "request_id": "request-old",
                            "disconnect_reason": None,
                        },
                    }
                ),
            )

            coordinator_class(store, pid_is_alive=lambda pid: False)

            state = store.load(TASK_ID)
            self.assertEqual(state["execution_state"], "PREPARING")
            self.assertEqual(state["wait_state"], "DETACHED")
            self.assertEqual(state["wait"]["disconnect_reason"], "stale-owner")

    def test_startup_keeps_attached_state_owned_by_a_live_process(self):
        coordinator_class = self.coordinator_class()
        with TemporaryDirectory() as tmp:
            store = self.make_store(Path(tmp))
            store.update(
                TASK_ID,
                lambda state: state.update(
                    {
                        "wait_state": "ATTACHED",
                        "wait": {
                            "owner_pid": os.getpid(),
                            "request_id": "request-live",
                            "disconnect_reason": None,
                        },
                    }
                ),
            )

            coordinator_class(store, pid_is_alive=lambda pid: True)

            state = store.load(TASK_ID)
            self.assertEqual(state["wait_state"], "ATTACHED")
            self.assertEqual(state["wait"]["request_id"], "request-live")

    def test_startup_does_not_repair_a_waiter_that_still_holds_the_file_lock(self):
        coordinator_class = self.coordinator_class()
        with TemporaryDirectory() as tmp:
            store = self.make_store(Path(tmp))
            store.update(
                TASK_ID,
                lambda state: state.update(
                    {
                        "wait_state": "ATTACHED",
                        "wait": {
                            "owner_pid": 424242,
                            "request_id": "request-active",
                            "disconnect_reason": None,
                        },
                    }
                ),
            )

            with TaskLock(store.task_dir(TASK_ID) / "wait.lock"):
                coordinator_class(store, pid_is_alive=lambda pid: False)

            self.assertEqual(store.load(TASK_ID)["wait_state"], "ATTACHED")


if __name__ == "__main__":
    unittest.main()
