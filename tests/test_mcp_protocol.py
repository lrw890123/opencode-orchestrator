import io
import json
import threading
import time
import unittest


INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}


class FakeCoordinator:
    def __init__(self):
        self.calls = []
        self.cancel_event = threading.Event()

    def cancel_request(self, request_id, reason):
        self.calls.append((request_id, reason))
        self.cancel_event.set()
        return True


class FakeToolService:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.started = threading.Event()

    def call(self, name, arguments, request_id, progress=None):
        if progress is not None:
            progress("OpenCode is working")
            progress("OpenCode is still working")
        if name == "blocking":
            self.started.set()
            self.coordinator.cancel_event.wait(timeout=2)
        return {
            "schema_version": 3,
            "task_id": "oc-20260830-010101-a1b2c3d4",
            "outcome": "COMPLETED",
            "execution_state": "COMPLETED",
            "wait_state": "DETACHED",
            "summary": f"called {name}",
            "next_action": "review_result",
            "artifacts": {"arguments": arguments, "request_id": request_id},
        }


class SecretErrorToolService(FakeToolService):
    def call(self, name, arguments, request_id, progress=None):
        from opencode_orchestrator.tool_service import ToolInputError

        raise ToolInputError(f"unknown tool: {name}; Authorization=Bearer tool-secret")


class MCPProtocolTest(unittest.TestCase):
    def make_server(self):
        from opencode_orchestrator.mcp_protocol import MCPProtocolServer

        output = io.StringIO()
        coordinator = FakeCoordinator()
        tools = FakeToolService(coordinator)
        server = MCPProtocolServer(tools, coordinator, output_stream=output)
        return server, output, tools, coordinator

    @staticmethod
    def messages(output):
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def initialize(self, server):
        server.handle(INITIALIZE)
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def test_initialize_echoes_protocol_and_lists_exact_tools(self):
        server, output, _, _ = self.make_server()
        try:
            self.initialize(server)
            server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            messages = self.messages(output)

            initialized = next(message for message in messages if message.get("id") == 1)
            self.assertEqual(initialized["result"]["protocolVersion"], "2025-06-18")
            self.assertFalse(initialized["result"]["capabilities"]["tools"]["listChanged"])
            self.assertEqual(initialized["result"]["serverInfo"]["version"], "2.1.5")
            listed = next(message for message in messages if message.get("id") == 2)
            self.assertEqual(len(listed["result"]["tools"]), 8)
            self.assertTrue(
                all(
                    tool["outputSchema"]["properties"]["schema_version"]["const"] == 3
                    for tool in listed["result"]["tools"]
                )
            )
            self.assertEqual(
                [tool["name"] for tool in listed["result"]["tools"]],
                [
                    "delegate_and_wait",
                    "reply_and_wait",
                    "resume_wait",
                    "task_status",
                    "read_transcript",
                    "collect_result",
                    "cancel_wait",
                    "abort_task",
                ],
            )
        finally:
            server.shutdown()

    def test_tool_call_before_initialized_notification_is_rejected(self):
        server, output, _, _ = self.make_server()
        try:
            server.handle(INITIALIZE)
            server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "task_status", "arguments": {}},
                }
            )
            response = next(message for message in self.messages(output) if message.get("id") == 2)
            self.assertEqual(response["error"]["code"], -32002)
        finally:
            server.shutdown()

    def test_tool_call_returns_text_and_structured_content(self):
        server, output, _, _ = self.make_server()
        try:
            self.initialize(server)
            server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "task_status", "arguments": {"task_id": "oc-test"}},
                }
            )
            self.assertTrue(server.wait_for_idle(timeout=2))
            response = next(message for message in self.messages(output) if message.get("id") == 3)
            self.assertFalse(response["result"]["isError"])
            self.assertEqual(response["result"]["structuredContent"]["schema_version"], 3)
            self.assertEqual(
                json.loads(response["result"]["content"][0]["text"]),
                response["result"]["structuredContent"],
            )
        finally:
            server.shutdown()

    def test_tool_call_emits_throttled_mcp_progress_when_client_supplies_token(self):
        server, output, _, _ = self.make_server()
        try:
            self.initialize(server)
            server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 31,
                    "method": "tools/call",
                    "params": {
                        "name": "task_status",
                        "arguments": {"task_id": "oc-test"},
                        "_meta": {"progressToken": "progress-31"},
                    },
                }
            )
            self.assertTrue(server.wait_for_idle(timeout=2))
            messages = self.messages(output)
            progress = [
                message
                for message in messages
                if message.get("method") == "notifications/progress"
            ]

            self.assertEqual(
                progress,
                [
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/progress",
                        "params": {
                            "progressToken": "progress-31",
                            "progress": 1,
                            "message": "OpenCode is working",
                        },
                    }
                ],
            )
            self.assertLess(messages.index(progress[0]), len(messages) - 1)
            self.assertEqual(messages[-1]["id"], 31)
        finally:
            server.shutdown()

    def test_tool_call_without_progress_token_emits_no_progress_notification(self):
        server, output, _, _ = self.make_server()
        try:
            self.initialize(server)
            server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 32,
                    "method": "tools/call",
                    "params": {
                        "name": "task_status",
                        "arguments": {"task_id": "oc-test"},
                    },
                }
            )
            self.assertTrue(server.wait_for_idle(timeout=2))

            self.assertFalse(
                any(
                    message.get("method") == "notifications/progress"
                    for message in self.messages(output)
                )
            )
        finally:
            server.shutdown()

    def test_unexpected_tool_error_does_not_echo_name_or_exception_text(self):
        from opencode_orchestrator.mcp_protocol import MCPProtocolServer

        output = io.StringIO()
        coordinator = FakeCoordinator()
        server = MCPProtocolServer(
            SecretErrorToolService(coordinator),
            coordinator,
            output_stream=output,
        )
        try:
            self.initialize(server)
            server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 30,
                    "method": "tools/call",
                    "params": {
                        "name": "secret-tool-name",
                        "arguments": {"authorization": "Bearer argument-secret"},
                    },
                }
            )
            self.assertTrue(server.wait_for_idle(timeout=2))
            response = next(message for message in self.messages(output) if message.get("id") == 30)
            serialized = json.dumps(response)

            self.assertEqual(response["error"]["code"], -32602)
            self.assertEqual(
                response["error"]["data"],
                {"name": "ToolInputError", "code": "invalid_arguments"},
            )
            self.assertNotIn("secret-tool-name", serialized)
            self.assertNotIn("tool-secret", serialized)
            self.assertNotIn("argument-secret", serialized)
            self.assertNotIn("Authorization", serialized)
        finally:
            server.shutdown()

    def test_cancel_notification_signals_request_and_suppresses_response(self):
        server, output, tools, coordinator = self.make_server()
        try:
            self.initialize(server)
            server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 9,
                    "method": "tools/call",
                    "params": {"name": "blocking", "arguments": {}},
                }
            )
            self.assertTrue(tools.started.wait(timeout=1))
            server.handle(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": 9, "reason": "client stopped"},
                }
            )
            self.assertTrue(server.wait_for_idle(timeout=2))

            self.assertEqual(coordinator.calls, [("9", "client stopped")])
            self.assertNotIn(9, [message.get("id") for message in self.messages(output)])
        finally:
            server.shutdown()

    def test_run_emits_parse_error_for_malformed_or_embedded_json(self):
        from opencode_orchestrator.mcp_protocol import MCPProtocolServer

        coordinator = FakeCoordinator()
        output = io.StringIO()
        server = MCPProtocolServer(FakeToolService(coordinator), coordinator)
        server.run(io.StringIO('{bad json}\n{} {}\n'), output)

        messages = self.messages(output)
        self.assertEqual([message["error"]["code"] for message in messages], [-32700, -32700])
        self.assertTrue(all(message["id"] is None for message in messages))

    def test_ping_works_after_initialization(self):
        server, output, _, _ = self.make_server()
        try:
            self.initialize(server)
            server.handle({"jsonrpc": "2.0", "id": "ping-1", "method": "ping"})
            response = next(message for message in self.messages(output) if message.get("id") == "ping-1")
            self.assertEqual(response["result"], {})
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main()
