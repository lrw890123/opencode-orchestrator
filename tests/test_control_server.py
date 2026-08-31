from copy import deepcopy
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import unittest

from opencode_orchestrator.service import BridgeService
from opencode_orchestrator.task_state import TaskStore
from opencode_orchestrator.tool_service import ToolService
from tests.test_git_workspace import create_repo
from tests.test_service_integration_unit import BlockingClient, ClientFactory, LOW_REQUEST


TASK_ID = "oc-20260830-010101-a1b2c3d4"


class ControlServerTest(unittest.TestCase):
    @staticmethod
    def mode(path: Path) -> int:
        return stat.S_IMODE(path.stat().st_mode)

    @staticmethod
    def raw_request(socket_path: Path, payload: dict) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2)
            connection.connect(str(socket_path))
            connection.sendall(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
                + b"\n"
            )
            response = b""
            while not response.endswith(b"\n"):
                chunk = connection.recv(65536)
                if not chunk:
                    break
                response += chunk
        return json.loads(response)

    def test_socket_directory_token_permissions_and_authentication(self):
        from opencode_orchestrator.control_cli import ControlClient
        from opencode_orchestrator.control_server import ControlServer

        with TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            store = TaskStore(state_root)
            store.create(TASK_ID, "/repo", "abc", "main", "clean")
            bridge = BridgeService(state_root)
            tool_service = ToolService(bridge)
            server = ControlServer(state_root, tool_service)
            server.start()
            try:
                store.update(
                    TASK_ID,
                    lambda state: state.update(
                        {
                            "wait_state": "ATTACHED",
                            "wait": {"owner_pid": server.owner_pid},
                        }
                    ),
                )
                self.assertEqual(self.mode(server.control_dir), 0o700)
                self.assertEqual(self.mode(server.socket_path), 0o600)
                self.assertEqual(self.mode(server.token_path), 0o600)

                unauthorized = self.raw_request(
                    server.socket_path,
                    {
                        "action": "status",
                        "task_id": TASK_ID,
                        "nonce": "nonce-wrong",
                        "token": "wrong-token",
                    },
                )
                self.assertEqual(unauthorized, {"ok": False, "error": "unauthorized"})

                missing_task = self.raw_request(
                    server.socket_path,
                    {
                        "action": "status",
                        "task_id": "",
                        "nonce": "nonce-missing-task",
                        "token": server.token,
                    },
                )
                self.assertEqual(missing_task["error"], "task-id-required")

                authorized = ControlClient(state_root).request("status", TASK_ID)
                self.assertTrue(authorized["ok"])
                self.assertNotIn("mode", authorized)
                self.assertEqual(authorized["result"]["task_id"], TASK_ID)
            finally:
                server.close()

            self.assertFalse(server.socket_path.exists())

    def test_payload_larger_than_64_kib_is_rejected(self):
        from opencode_orchestrator.control_server import ControlServer

        with TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            store = TaskStore(state_root)
            store.create(TASK_ID, "/repo", "abc", "main", "clean")
            bridge = BridgeService(state_root)
            server = ControlServer(state_root, ToolService(bridge))
            server.start()
            try:
                response = self.raw_request(
                    server.socket_path,
                    {
                        "action": "status",
                        "task_id": TASK_ID,
                        "nonce": "x" * (65 * 1024),
                        "token": server.token,
                    },
                )
                self.assertEqual(response["error"], "payload-too-large")
            finally:
                server.close()

    def test_long_state_root_uses_a_short_protected_socket_path(self):
        from opencode_orchestrator.control_cli import ControlClient
        from opencode_orchestrator.control_server import ControlServer

        with TemporaryDirectory() as tmp:
            state_root = Path(tmp) / ("long-state-root-" + "x" * 100) / "state"
            store = TaskStore(state_root)
            store.create(TASK_ID, "/repo", "abc", "main", "clean")
            bridge = BridgeService(state_root)
            server = ControlServer(state_root, ToolService(bridge))

            server.start()
            try:
                store.update(
                    TASK_ID,
                    lambda state: state.update(
                        {
                            "wait_state": "ATTACHED",
                            "wait": {"owner_pid": server.owner_pid},
                        }
                    ),
                )
                self.assertTrue(str(server.socket_path).startswith("/tmp/"))
                self.assertLess(len(os.fsencode(server.socket_path)), 100)
                self.assertEqual(self.mode(server.socket_path.parent), 0o700)
                response = ControlClient(state_root).request("status", TASK_ID)
                self.assertTrue(response["ok"])
                self.assertNotIn("mode", response)
            finally:
                server.close()

    def test_external_cancel_releases_pending_tool_without_aborting_opencode(self):
        from opencode_orchestrator.control_cli import ControlClient
        from opencode_orchestrator.control_server import ControlServer

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = create_repo(root / "source")
            client = BlockingClient()
            bridge = BridgeService(root / "state", client_factory=ClientFactory(client))
            tool_service = ToolService(bridge)
            server = ControlServer(root / "state", tool_service)
            server.start()
            result = {}
            errors = []

            def delegate():
                try:
                    result.update(
                        tool_service.call(
                            "delegate_and_wait",
                            {
                                "repo_path": str(source),
                                "task_contract": deepcopy(LOW_REQUEST),
                                "timeout_seconds": 10,
                            },
                            "mcp-request-1",
                        )
                    )
                except Exception as error:
                    errors.append(error)

            thread = threading.Thread(target=delegate)
            thread.start()
            try:
                self.assertTrue(client.prompted.wait(timeout=2))
                task_id = next(path.name for path in bridge.store.tasks_root.iterdir() if path.is_dir())

                cancelled = ControlClient(root / "state").request("cancel-wait", task_id)
                thread.join(timeout=2)

                self.assertTrue(cancelled["ok"])
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(result["outcome"], "WAIT_CANCELLED")
                self.assertEqual(client.abort_count, 0)
            finally:
                server.close()
                if thread.is_alive():
                    bridge.wait_coordinator.cancel_request("mcp-request-1", "test-cleanup")
                    thread.join(timeout=2)

    def test_cancel_re_resolves_owner_after_wait_handoff(self):
        from opencode_orchestrator.control_cli import ControlClient
        from opencode_orchestrator.control_server import (
            ControlServer,
            control_socket_path,
            control_token_path,
        )

        with TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            store = TaskStore(state_root)
            store.create(TASK_ID, "/repo", "abc", "main", "clean")
            first_owner = 41001
            second_owner = 41002

            def attach(owner_pid: int) -> None:
                store.update(
                    TASK_ID,
                    lambda state: state.update(
                        {
                            "execution_state": "RUNNING",
                            "wait_state": "ATTACHED",
                            "wait": {
                                "owner_pid": owner_pid,
                                "request_id": f"request-{owner_pid}",
                            },
                        }
                    ),
                )

            class HandoffService:
                def __init__(self, result: dict, on_call=None) -> None:
                    self.result = result
                    self.on_call = on_call

                def call(self, name: str, arguments: dict, request_id: str) -> dict:
                    if self.on_call is not None:
                        self.on_call()
                    return self.result

            first = ControlServer(
                state_root,
                HandoffService(
                    {"outcome": "INTERRUPTED", "cancelled": False},
                    on_call=lambda: attach(second_owner),
                ),
            )
            first.owner_pid = first_owner
            first.socket_path = control_socket_path(state_root, first_owner)
            first.token_path = control_token_path(state_root, first_owner)
            second = ControlServer(
                state_root,
                HandoffService({"outcome": "WAIT_CANCELLED", "cancelled": True}),
            )
            second.owner_pid = second_owner
            second.socket_path = control_socket_path(state_root, second_owner)
            second.token_path = control_token_path(state_root, second_owner)
            first.start()
            second.start()
            try:
                attach(first_owner)

                result = ControlClient(state_root).request("cancel-wait", TASK_ID)

                self.assertTrue(result["ok"])
                self.assertEqual(result["result"]["outcome"], "WAIT_CANCELLED")
                self.assertTrue(result["result"]["cancelled"])
            finally:
                first.close()
                second.close()

    def test_offline_cancel_repairs_only_a_stale_attached_wait(self):
        from opencode_orchestrator.control_cli import ControlClient

        with TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            store = TaskStore(state_root)
            store.create(TASK_ID, "/repo", "abc", "main", "clean")
            store.update(
                TASK_ID,
                lambda state: state.update(
                    {
                        "execution_state": "RUNNING",
                        "wait_state": "ATTACHED",
                        "wait": {"owner_pid": 424242, "request_id": "old-request"},
                    }
                ),
            )

            result = ControlClient(state_root, pid_is_alive=lambda pid: False).request(
                "cancel-wait", TASK_ID
            )

            state = store.load(TASK_ID)
            self.assertTrue(result["ok"])
            self.assertEqual(result["mode"], "offline")
            self.assertEqual(state["execution_state"], "RUNNING")
            self.assertEqual(state["wait_state"], "DETACHED")
            self.assertEqual(state["wait"]["disconnect_reason"], "offline-stale")

    def test_offline_abort_failure_preserves_abort_intent(self):
        from opencode_orchestrator.control_cli import ControlClient

        with TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            store = TaskStore(state_root)
            store.create(TASK_ID, "/repo", "abc", "main", "clean")
            store.update(
                TASK_ID,
                lambda state: state.update(
                    {
                        "execution_state": "RUNNING",
                        "opencode": {
                            "base_url": "http://127.0.0.1:1",
                            "directory": "/repo",
                            "session_id": "ses_missing",
                        },
                    }
                ),
            )

            result = ControlClient(state_root).request("abort-task", TASK_ID)

            self.assertFalse(result["ok"])
            self.assertEqual(store.load(TASK_ID)["abort_intent"]["state"], "REQUESTED")

    def test_control_cli_bootstrap_reads_offline_status(self):
        with TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            TaskStore(state_root).create(TASK_ID, "/repo", "abc", "main", "clean")
            script = Path(__file__).parents[1] / "bin/oc-control"

            completed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "status",
                    "--state-root",
                    str(state_root),
                    "--task-id",
                    TASK_ID,
                ],
                text=True,
                capture_output=True,
                timeout=3,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["mode"], "offline")


if __name__ == "__main__":
    unittest.main()
