import json
from pathlib import Path
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest

from tests.support.mcp_client import MCPSubprocessClient
from tests.support.fake_opencode import FakeOpenCodeServer
from tests.test_mcp_protocol import INITIALIZE
from tests.test_git_workspace import create_repo
from tests.test_service_integration_unit import LOW_REQUEST
from opencode_orchestrator.task_state import TaskStore


ROOT = Path(__file__).parents[1]
SERVER = ROOT / "mcp/server.py"
CONTROL = ROOT / "bin/oc-control"


class MCPServerE2ETest(unittest.TestCase):
    def start_client(self, state_root: Path) -> MCPSubprocessClient:
        client = MCPSubprocessClient(SERVER, state_root=state_root)
        client.request(INITIALIZE)
        client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return client

    @staticmethod
    def delegate_call(request_id: int, source: Path, server: FakeOpenCodeServer) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
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

    @staticmethod
    def wait_for_prompt(server: FakeOpenCodeServer, timeout: float = 3) -> None:
        deadline = time.monotonic() + timeout
        with server.condition:
            while server.prompt_count < 1 and time.monotonic() < deadline:
                server.condition.wait(timeout=0.05)
        if server.prompt_count < 1:
            raise AssertionError("OpenCode prompt was not dispatched")

    @staticmethod
    def only_task_id(state_root: Path) -> str:
        return next(path.name for path in (state_root / "tasks").iterdir() if path.is_dir())

    def test_subprocess_handshake_lists_tools_and_exits_on_stdin_close(self):
        with TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            client = MCPSubprocessClient(SERVER, state_root=state_root)
            try:
                initialized = client.request(INITIALIZE)
                self.assertEqual(initialized["result"]["protocolVersion"], "2025-06-18")
                client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                deadline = time.monotonic() + 2
                socket_paths = []
                while not socket_paths and time.monotonic() < deadline:
                    socket_paths = list((state_root / "control").glob("server-*.sock"))
                    time.sleep(0.01)
                self.assertEqual(len(socket_paths), 1)
                socket_path = socket_paths[0]
                listed = client.request(
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
                )
                self.assertEqual(len(listed["result"]["tools"]), 8)
            finally:
                remaining_stdout, stderr = client.close()

            for line in remaining_stdout.splitlines():
                json.loads(line)
            self.assertEqual(client.process.returncode, 0, stderr)
            self.assertFalse(socket_path.exists())

    def test_delegate_stays_pending_without_model_polling_until_idle_gate_opens(self):
        with TemporaryDirectory() as tmp, FakeOpenCodeServer("delayed_idle") as server:
            root = Path(tmp)
            state_root = root / "state"
            source = create_repo(root / "source")
            client = self.start_client(state_root)
            try:
                client.send(self.delegate_call(10, source, server))
                self.wait_for_prompt(server)

                time.sleep(0.5)
                self.assertFalse(client.has_response(timeout=0.05))
                self.assertEqual(server.prompt_count, 1)
                self.assertEqual(server.status_request_count, 0)
                self.assertEqual(client.sent_request_count, 2)

                server.release_idle.set()
                with server.condition:
                    server.condition.notify_all()
                response = client.receive(timeout=3)

                self.assertEqual(response["id"], 10)
                self.assertEqual(
                    response["result"]["structuredContent"]["outcome"],
                    "COMPLETED",
                )
                self.assertEqual(server.prompt_count, 1)
            finally:
                client.close()

    def test_delegate_streams_mcp_progress_without_extra_requests(self):
        with TemporaryDirectory() as tmp, FakeOpenCodeServer("delayed_idle") as server:
            root = Path(tmp)
            state_root = root / "state"
            source = create_repo(root / "source")
            client = self.start_client(state_root)
            try:
                call = self.delegate_call(14, source, server)
                call["params"]["_meta"] = {"progressToken": "progress-14"}
                client.send(call)

                notification = client.receive(timeout=3)
                self.assertEqual(notification["method"], "notifications/progress")
                self.assertEqual(
                    notification["params"],
                    {
                        "progressToken": "progress-14",
                        "progress": 1,
                        "message": "OpenCode task is starting",
                    },
                )
                self.wait_for_prompt(server)
                self.assertEqual(server.status_request_count, 0)
                self.assertEqual(client.sent_request_count, 2)

                server.release_idle.set()
                with server.condition:
                    server.condition.notify_all()
                response = client.receive(timeout=3)
                while response.get("id") != 14:
                    self.assertEqual(response.get("method"), "notifications/progress")
                    response = client.receive(timeout=3)

                self.assertEqual(
                    response["result"]["structuredContent"]["outcome"],
                    "COMPLETED",
                )
            finally:
                client.close()

    def test_continue_reuses_the_same_task_session_and_public_tool(self):
        with TemporaryDirectory() as tmp, FakeOpenCodeServer("idle") as server:
            root = Path(tmp)
            state_root = root / "state"
            source = create_repo(root / "source")
            client = self.start_client(state_root)
            try:
                initial = client.request(self.delegate_call(15, source, server), timeout=5)
                self.assertEqual(
                    initial["result"]["structuredContent"]["outcome"],
                    "COMPLETED",
                )
                task_id = self.only_task_id(state_root)
                store = TaskStore(state_root)

                def pause(state):
                    state["execution_state"] = "RUNNING"
                    state["phase"] = "PAUSED"

                store.update(task_id, pause)
                session_id = store.load(task_id)["opencode"]["session_id"]

                continued = client.request(
                    {
                        "jsonrpc": "2.0",
                        "id": 16,
                        "method": "tools/call",
                        "params": {
                            "name": "reply_and_wait",
                            "arguments": {
                                "task_id": task_id,
                                "kind": "continue",
                                "payload": {
                                    "text": "Continue the same approved task."
                                },
                                "timeout_seconds": 5,
                            },
                        },
                    },
                    timeout=7,
                )

                self.assertEqual(
                    continued["result"]["structuredContent"]["outcome"],
                    "COMPLETED",
                )
                self.assertEqual(server.created_session_count, 1)
                self.assertEqual(server.prompt_count, 2)
                session = server.sessions[session_id]
                continuation_text = session.prompts[-1]["parts"][0]["text"]
                self.assertIn(f"[oc-task:{task_id}] continuation 1", continuation_text)
                self.assertEqual(session.prompts[-1]["variant"], "max")
                final_state = store.load(task_id)
                self.assertEqual(
                    final_state["execution"]["continuation"]["dispatch_state"],
                    "SENT",
                )
            finally:
                client.close()

    def test_external_turn_reacquires_completed_and_aborted_task_over_public_tools(self):
        with TemporaryDirectory() as tmp, FakeOpenCodeServer("idle") as server:
            root = Path(tmp)
            state_root = root / "state"
            source = create_repo(root / "source")
            client = self.start_client(state_root)
            try:
                initial = client.request(self.delegate_call(40, source, server), timeout=5)
                self.assertEqual(
                    initial["result"]["structuredContent"]["outcome"],
                    "COMPLETED",
                )
                task_id = self.only_task_id(state_root)
                store = TaskStore(state_root)
                session_id = store.load(task_id)["opencode"]["session_id"]
                session = server.sessions[session_id]

                client.request(
                    {
                        "jsonrpc": "2.0",
                        "id": 41,
                        "method": "tools/call",
                        "params": {
                            "name": "collect_result",
                            "arguments": {"task_id": task_id},
                        },
                    },
                    timeout=5,
                )
                with server.condition:
                    session.busy = True
                    session.pending_permissions = [
                        {
                            "id": "per-external-turn",
                            "sessionID": session_id,
                            "action": "external_directory",
                            "resources": ["/tmp/*"],
                        }
                    ]
                    server.activity += 1
                    server.condition.notify_all()

                status = client.request(
                    {
                        "jsonrpc": "2.0",
                        "id": 42,
                        "method": "tools/call",
                        "params": {
                            "name": "task_status",
                            "arguments": {"task_id": task_id},
                        },
                    },
                    timeout=5,
                )["result"]["structuredContent"]
                self.assertEqual(status["execution_state"], "INPUT_REQUIRED")
                self.assertEqual(
                    status["artifacts"]["state"]["phase"],
                    "PERMISSION_WAIT",
                )

                replied = client.request(
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
                                    "request_id": "per-external-turn",
                                    "response": "once",
                                    "user_approved": True,
                                    "approval_basis": (
                                        "Approve external_directory /tmp/* for this task."
                                    ),
                                    "remember_for_task": True,
                                },
                                "timeout_seconds": 5,
                            },
                        },
                    },
                    timeout=7,
                )["result"]["structuredContent"]
                self.assertEqual(replied["outcome"], "COMPLETED")
                self.assertEqual(
                    session.permission_replies[-1],
                    {"request_id": "per-external-turn", "body": {"reply": "once"}},
                )
                self.assertEqual(
                    store.load(task_id)["task_permission_rules"],
                    [
                        {
                            "permission": "external_directory",
                            "pattern": "/tmp/*",
                            "action": "allow",
                        }
                    ],
                )

                aborted = client.request(
                    {
                        "jsonrpc": "2.0",
                        "id": 44,
                        "method": "tools/call",
                        "params": {
                            "name": "abort_task",
                            "arguments": {"task_id": task_id},
                        },
                    },
                    timeout=5,
                )["result"]["structuredContent"]
                self.assertEqual(aborted["outcome"], "ABORTED")
                with server.condition:
                    session.busy = True
                    server.activity += 1
                    server.condition.notify_all()

                aborted_status = client.request(
                    {
                        "jsonrpc": "2.0",
                        "id": 45,
                        "method": "tools/call",
                        "params": {
                            "name": "task_status",
                            "arguments": {"task_id": task_id},
                        },
                    },
                    timeout=5,
                )["result"]["structuredContent"]
                self.assertEqual(aborted_status["execution_state"], "RUNNING")

                resumed = client.request(
                    {
                        "jsonrpc": "2.0",
                        "id": 46,
                        "method": "tools/call",
                        "params": {
                            "name": "resume_wait",
                            "arguments": {
                                "task_id": task_id,
                                "timeout_seconds": 5,
                            },
                        },
                    },
                    timeout=7,
                )["result"]["structuredContent"]
                final_state = store.load(task_id)

                self.assertEqual(resumed["outcome"], "COMPLETED")
                self.assertEqual(final_state["abort"]["state"], "SUPERSEDED")
                self.assertEqual(final_state["opencode"]["session_id"], session_id)
                self.assertEqual(server.created_session_count, 1)
                self.assertEqual(server.prompt_count, 1)
            finally:
                client.close()

    def test_external_cancel_returns_wait_cancelled_without_aborting(self):
        with TemporaryDirectory() as tmp, FakeOpenCodeServer("blocking") as server:
            root = Path(tmp)
            state_root = root / "state"
            source = create_repo(root / "source")
            client = self.start_client(state_root)
            try:
                client.send(self.delegate_call(11, source, server))
                self.wait_for_prompt(server)
                task_id = self.only_task_id(state_root)

                cancelled = subprocess.run(
                    [
                        sys.executable,
                        str(CONTROL),
                        "cancel-wait",
                        "--state-root",
                        str(state_root),
                        "--task-id",
                        task_id,
                    ],
                    text=True,
                    capture_output=True,
                    timeout=3,
                )
                response = client.receive(timeout=3)

                self.assertEqual(cancelled.returncode, 0, cancelled.stderr or cancelled.stdout)
                self.assertEqual(response["id"], 11)
                self.assertEqual(
                    response["result"]["structuredContent"]["outcome"],
                    "WAIT_CANCELLED",
                )
                self.assertEqual(server.abort_count, 0)
                self.assertTrue(next(iter(server.sessions.values())).busy)
            finally:
                server.release_idle.set()
                with server.condition:
                    server.condition.notify_all()
                client.close()

    def test_queued_safe_permissions_reconcile_to_completion_once(self):
        with TemporaryDirectory() as tmp, FakeOpenCodeServer("queued_permissions") as server:
            root = Path(tmp)
            state_root = root / "state"
            source = create_repo(root / "source")
            client = self.start_client(state_root)
            try:
                client.send(self.delegate_call(30, source, server))
                response = client.receive(timeout=10)

                self.assertEqual(response["id"], 30)
                result = response["result"]["structuredContent"]
                self.assertEqual(result["outcome"], "COMPLETED")
                self.assertEqual(server.created_session_count, 1)
                self.assertEqual(server.prompt_count, 1)
                session = next(iter(server.sessions.values()))
                self.assertEqual(
                    [item["request_id"] for item in session.permission_replies],
                    ["per_1", "per_2", "per_3"],
                )
                self.assertEqual(
                    [item["body"] for item in session.permission_replies],
                    [{"reply": "once"}] * 3,
                )
            finally:
                client.close()

    def test_queued_external_permission_requires_rule_then_completes_with_rule(self):
        with TemporaryDirectory() as tmp, FakeOpenCodeServer("queued_external_permissions") as server:
            root = Path(tmp)
            state_root = root / "state"
            source = create_repo(root / "source")
            client = self.start_client(state_root)
            try:
                client.send(self.delegate_call(31, source, server))
                response = client.receive(timeout=10)
                result = response["result"]["structuredContent"]
                self.assertEqual(result["outcome"], "INPUT_REQUIRED")
                self.assertEqual(result["event"]["properties"]["action"], "external_directory")
                self.assertEqual(server.prompt_count, 1)
            finally:
                client.close()

        with TemporaryDirectory() as tmp, FakeOpenCodeServer("queued_external_permissions") as server:
            root = Path(tmp)
            state_root = root / "state"
            source = create_repo(root / "source")
            client = self.start_client(state_root)
            try:
                call = self.delegate_call(32, source, server)
                call["params"]["arguments"]["permission_policy"] = {
                    "rules": [
                        {
                            "permission": "external_directory",
                            "pattern": "/old/worktree/**",
                            "action": "allow",
                        }
                    ]
                }
                client.send(call)
                response = client.receive(timeout=10)
                result = response["result"]["structuredContent"]
                self.assertEqual(result["outcome"], "COMPLETED")
                self.assertEqual(server.created_session_count, 1)
                self.assertEqual(server.prompt_count, 1)
                session = next(iter(server.sessions.values()))
                self.assertEqual(len(session.permission_replies), 2)
                self.assertEqual(
                    [item["body"] for item in session.permission_replies],
                    [{"reply": "once"}] * 2,
                )
            finally:
                client.close()

    def test_heartbeat_only_busy_task_becomes_stalled_without_abort(self):
        with TemporaryDirectory() as tmp, FakeOpenCodeServer("heartbeat_only") as server:
            root = Path(tmp)
            state_root = root / "state"
            source = create_repo(root / "source")
            client = self.start_client(state_root)
            try:
                call = self.delegate_call(33, source, server)
                call["params"]["arguments"]["progress_policy"] = {
                    "input_probe_interval_seconds": 5,
                    "stall_timeout_seconds": 30,
                }
                client.send(call)
                self.wait_for_prompt(server)
                task_id = self.only_task_id(state_root)
                state_path = state_root / "tasks" / task_id / "state.json"
                TaskStore(state_root).update(
                    task_id,
                    lambda state: state["progress"].update(
                        {
                            "last_progress_at": "2000-01-01T00:00:00+00:00",
                            "last_input_probe_at": "2000-01-01T00:00:00+00:00",
                        }
                    ),
                )
                response = client.receive(timeout=12)

                result = response["result"]["structuredContent"]
                self.assertEqual(result["outcome"], "STALLED")
                self.assertEqual(result["next_action"], "inspect_stall")
                self.assertEqual(server.abort_count, 0)
                progress = json.loads(state_path.read_text())["progress"]
                self.assertEqual(progress["pending_tools"][0]["name"], "read")
                self.assertEqual(progress["pending_tools"][0]["part_id"], "part-stall")
            finally:
                client.close()

    def test_external_cancel_routes_to_wait_owner_with_two_mcp_servers(self):
        with TemporaryDirectory() as tmp, FakeOpenCodeServer("blocking") as server:
            root = Path(tmp)
            state_root = root / "state"
            source = create_repo(root / "source")
            owner = self.start_client(state_root)
            competing = self.start_client(state_root)
            try:
                deadline = time.monotonic() + 2
                sockets = []
                tokens = []
                while (len(sockets) < 2 or len(tokens) < 2) and time.monotonic() < deadline:
                    sockets = list((state_root / "control").glob("server-*.sock"))
                    tokens = list((state_root / "control").glob("token-*"))
                    time.sleep(0.01)
                self.assertEqual(len(sockets), 2)
                self.assertEqual(len(tokens), 2)
                self.assertEqual(len({path.read_text() for path in tokens}), 2)
                for path in [*sockets, *tokens]:
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

                owner.send(self.delegate_call(13, source, server))
                self.wait_for_prompt(server)
                task_id = self.only_task_id(state_root)

                cancelled = subprocess.run(
                    [
                        sys.executable,
                        str(CONTROL),
                        "cancel-wait",
                        "--state-root",
                        str(state_root),
                        "--task-id",
                        task_id,
                    ],
                    text=True,
                    capture_output=True,
                    timeout=5,
                )

                self.assertEqual(cancelled.returncode, 0, cancelled.stderr or cancelled.stdout)
                payload = json.loads(cancelled.stdout)
                self.assertEqual(payload["result"]["outcome"], "WAIT_CANCELLED")
                response = owner.receive(timeout=3)
                self.assertEqual(response["id"], 13)
                self.assertEqual(
                    response["result"]["structuredContent"]["outcome"],
                    "WAIT_CANCELLED",
                )
                self.assertEqual(server.abort_count, 0)
                self.assertTrue(next(iter(server.sessions.values())).busy)
            finally:
                server.release_idle.set()
                with server.condition:
                    server.condition.notify_all()
                owner.close()
                competing.close()
            self.assertEqual(
                list((state_root / "control").glob("server-*.sock")),
                [],
            )
            self.assertEqual(list((state_root / "control").glob("token-*")), [])

    def test_external_abort_calls_opencode_once_and_preserves_worktree(self):
        with TemporaryDirectory() as tmp, FakeOpenCodeServer("blocking") as server:
            root = Path(tmp)
            state_root = root / "state"
            source = create_repo(root / "source")
            client = self.start_client(state_root)
            try:
                client.send(self.delegate_call(12, source, server))
                self.wait_for_prompt(server)
                task_id = self.only_task_id(state_root)

                aborted = subprocess.run(
                    [
                        sys.executable,
                        str(CONTROL),
                        "abort-task",
                        "--state-root",
                        str(state_root),
                        "--task-id",
                        task_id,
                    ],
                    text=True,
                    capture_output=True,
                    timeout=3,
                )
                payload = json.loads(aborted.stdout)
                state = json.loads((state_root / "tasks" / task_id / "state.json").read_text())

                self.assertEqual(aborted.returncode, 0, aborted.stderr or aborted.stdout)
                self.assertEqual(payload["result"]["outcome"], "ABORTED")
                self.assertEqual(server.abort_count, 1)
                self.assertEqual(state["execution_state"], "ABORTED")
                self.assertTrue(Path(state["worktree"]["path"]).is_dir())
                client.receive(timeout=3)
            finally:
                server.release_idle.set()
                with server.condition:
                    server.condition.notify_all()
                client.close()


if __name__ == "__main__":
    unittest.main()
