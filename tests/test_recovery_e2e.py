import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

from tests.support.fake_opencode import FakeOpenCodeServer
from tests.support.mcp_client import MCPSubprocessClient
from tests.test_git_workspace import create_repo
from tests.test_mcp_protocol import INITIALIZE
from tests.test_mcp_server_e2e import SERVER
from tests.test_service_integration_unit import LOW_REQUEST


class RecoveryE2ETest(unittest.TestCase):
    @staticmethod
    def start_client(state_root: Path) -> MCPSubprocessClient:
        client = MCPSubprocessClient(SERVER, state_root=state_root)
        client.request(INITIALIZE)
        client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return client

    def test_killed_mcp_process_resumes_same_task_without_duplicate_prompt(self):
        with TemporaryDirectory() as tmp, FakeOpenCodeServer("delayed_idle") as server:
            root = Path(tmp)
            state_root = root / "state"
            source = create_repo(root / "source")
            first = self.start_client(state_root)
            first.send(
                {
                    "jsonrpc": "2.0",
                    "id": 20,
                    "method": "tools/call",
                    "params": {
                        "name": "delegate_and_wait",
                        "arguments": {
                            "repo_path": str(source),
                            "task_contract": LOW_REQUEST,
                            "server_url": server.base_url,
                            "timeout_seconds": 10,
                        },
                    },
                }
            )
            deadline = time.monotonic() + 3
            with server.condition:
                while server.prompt_count < 1 and time.monotonic() < deadline:
                    server.condition.wait(timeout=0.05)
            self.assertEqual(server.prompt_count, 1)
            task_id = next(
                path.name for path in (state_root / "tasks").iterdir() if path.is_dir()
            )

            first.kill()
            self.assertTrue(next(iter(server.sessions.values())).busy)

            second = self.start_client(state_root)
            try:
                second.send(
                    {
                        "jsonrpc": "2.0",
                        "id": 21,
                        "method": "tools/call",
                        "params": {
                            "name": "resume_wait",
                            "arguments": {"task_id": task_id, "timeout_seconds": 10},
                        },
                    }
                )
                deadline = time.monotonic() + 3
                while server.event_connection_count < 2 and time.monotonic() < deadline:
                    time.sleep(0.02)
                server.release_idle.set()
                with server.condition:
                    server.condition.notify_all()
                response = second.receive(timeout=3)

                self.assertEqual(response["id"], 21)
                self.assertEqual(
                    response["result"]["structuredContent"]["outcome"],
                    "COMPLETED",
                )
                self.assertEqual(server.prompt_count, 1)
                state = json.loads(
                    (state_root / "tasks" / task_id / "state.json").read_text()
                )
                self.assertEqual(state["execution_state"], "COMPLETED")
                self.assertEqual(state["wait_state"], "DETACHED")
            finally:
                second.close()

    def test_resume_and_reply_reconcile_next_queued_permission_without_duplicate_prompt(self):
        with TemporaryDirectory() as tmp, FakeOpenCodeServer("queued_external_permissions") as server:
            root = Path(tmp)
            state_root = root / "state"
            source = create_repo(root / "source")
            client = self.start_client(state_root)
            try:
                client.send(
                    {
                        "jsonrpc": "2.0",
                        "id": 40,
                        "method": "tools/call",
                        "params": {
                            "name": "delegate_and_wait",
                            "arguments": {
                                "repo_path": str(source),
                                "task_contract": LOW_REQUEST,
                                "server_url": server.base_url,
                                "timeout_seconds": 10,
                            },
                        },
                    }
                )
                first = client.receive(timeout=5)
                initial = first["result"]["structuredContent"]
                task_id = initial["task_id"]
                session_id = initial["session_id"]
                self.assertEqual(initial["outcome"], "INPUT_REQUIRED")

                client.send(
                    {
                        "jsonrpc": "2.0",
                        "id": 41,
                        "method": "tools/call",
                        "params": {
                            "name": "resume_wait",
                            "arguments": {"task_id": task_id, "timeout_seconds": 10},
                        },
                    }
                )
                resumed = client.receive(timeout=5)["result"]["structuredContent"]
                self.assertEqual(resumed["outcome"], "INPUT_REQUIRED")
                self.assertEqual(resumed["session_id"], session_id)
                first_request_id = resumed["event"]["properties"]["id"]
                first_resource = resumed["event"]["properties"]["resources"][0]

                client.send(
                    {
                        "jsonrpc": "2.0",
                        "id": 42,
                        "method": "tools/call",
                        "params": {
                            "name": "reply_and_wait",
                            "arguments": {
                                "task_id": task_id,
                                "kind": "permission",
                                "payload": {
                                    "request_id": first_request_id,
                                    "response": "once",
                                    "user_approved": True,
                                    "approval_basis": (
                                        "User approved external_directory once for "
                                        f"{first_resource}"
                                    ),
                                },
                                "timeout_seconds": 10,
                            },
                        },
                    }
                )
                next_permission = client.receive(timeout=5)["result"]["structuredContent"]
                self.assertEqual(next_permission["outcome"], "INPUT_REQUIRED")
                self.assertEqual(next_permission["session_id"], session_id)
                second_request_id = next_permission["event"]["properties"]["id"]
                second_resource = next_permission["event"]["properties"]["resources"][0]
                self.assertNotEqual(first_request_id, second_request_id)

                client.send(
                    {
                        "jsonrpc": "2.0",
                        "id": 43,
                        "method": "tools/call",
                        "params": {
                            "name": "reply_and_wait",
                            "arguments": {
                                "task_id": task_id,
                                "kind": "permission",
                                "payload": {
                                    "request_id": second_request_id,
                                    "response": "once",
                                    "user_approved": True,
                                    "approval_basis": (
                                        "User approved external_directory once for "
                                        f"{second_resource}"
                                    ),
                                },
                                "timeout_seconds": 10,
                            },
                        },
                    }
                )
                completed = client.receive(timeout=5)["result"]["structuredContent"]
                self.assertEqual(completed["outcome"], "COMPLETED")
                self.assertEqual(completed["task_id"], task_id)
                self.assertEqual(completed["session_id"], session_id)
                self.assertEqual(server.created_session_count, 1)
                self.assertEqual(server.prompt_count, 1)
            finally:
                client.close()


if __name__ == "__main__":
    unittest.main()
