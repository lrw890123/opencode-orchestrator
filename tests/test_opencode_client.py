import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import unittest
from urllib.parse import parse_qs, urlparse

from opencode_orchestrator.opencode_client import OpenCodeClient, OpenCodeError


class ContractHandler(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, format, *args):
        return

    def record(self, body=None):
        parsed = urlparse(self.path)
        self.__class__.requests.append(
            {
                "method": self.command,
                "path": parsed.path,
                "query": parse_qs(parsed.query),
                "body": body,
                "authorization": self.headers.get("Authorization"),
            }
        )
        return parsed

    def send_json(self, payload, status=200):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = self.record()
        if parsed.path == "/global/health":
            self.send_json({"healthy": True, "version": "test"})
        elif parsed.path == "/doc":
            self.send_json(
                {
                    "paths": {
                        "/api/session/{sessionID}/permission": {},
                        "/api/session/{sessionID}/question": {},
                        "/permission/{requestID}/reply": {},
                        "/question/{requestID}/reply": {},
                    }
                }
            )
        elif parsed.path == "/provider":
            self.send_json(
                {
                    "all": [
                        {
                            "id": "mcli",
                            "name": "mcli",
                            "source": "config",
                            "env": [],
                            "options": {},
                            "models": {
                                "glm-5.3": {
                                    "id": "glm-5.3",
                                    "providerID": "mcli",
                                    "name": "GLM 5.3",
                                    "status": "active",
                                    "variants": {"fast": {}, "high": {}, "max": {}},
                                }
                            },
                        }
                    ],
                    "connected": ["mcli"],
                }
            )
        elif parsed.path == "/session/status":
            self.send_json({"ses_new": {"type": "busy"}})
        elif parsed.path == "/session/ses_new":
            self.send_json({"id": "ses_new", "directory": "/tmp/工作 tree"})
        elif parsed.path == "/session/ses_new/message":
            self.send_json(
                [
                    {
                        "info": {"role": "assistant"},
                        "parts": [{"type": "text", "text": "done"}],
                    }
                ]
            )
        elif parsed.path == "/session/ses_new/diff":
            self.send_json([{"file": "math_utils.py"}])
        elif parsed.path == "/api/session/ses_new/permission":
            self.send_json(
                {
                    "data": [
                        {
                            "id": "per_v2",
                            "sessionID": "ses_new",
                            "action": "read",
                            "resources": ["README.md"],
                        }
                    ]
                }
            )
        elif parsed.path == "/api/session/ses_new/question":
            self.send_json(
                {
                    "data": [
                        {
                            "id": "que_v2",
                            "sessionID": "ses_new",
                            "questions": [
                                {
                                    "header": "Choice",
                                    "question": "Continue?",
                                    "options": [
                                        {"label": "Yes", "description": "Continue safely"}
                                    ],
                                }
                            ],
                        }
                    ]
                }
            )
        elif parsed.path == "/event":
            data = b'data: {"type":"server.connected","properties":{}}\n\n'
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif parsed.path == "/secret-error":
            self.send_json(
                {
                    "error": "Authorization: Bearer response-secret",
                    "credential": "response-secret",
                },
                status=502,
            )
        else:
            self.send_json({"error": "not found"}, status=404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        body = json.loads(raw) if raw else None
        parsed = self.record(body)
        if parsed.path == "/session":
            self.send_json({"id": "ses_new", "title": body["title"]})
        elif parsed.path == "/session/ses_new/prompt_async":
            self.send_response(204)
            self.end_headers()
        elif parsed.path == "/session/ses_new/abort":
            self.send_json(True)
        elif parsed.path in {"/permission/per_1/reply", "/question/que_1/reply"}:
            self.send_json(True)
        else:
            self.send_json({"error": "not found"}, status=404)


class OpenCodeClientTest(unittest.TestCase):
    def setUp(self):
        ContractHandler.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ContractHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_client_sends_scoped_requests_and_exact_payloads(self):
        directory = Path("/tmp/工作 tree")
        client = OpenCodeClient(self.base_url, directory)

        self.assertTrue(client.health()["healthy"])
        self.assertIn("/permission/{requestID}/reply", client.openapi_paths())
        session = client.create_session("[oc-task:test] demo")
        client.prompt_async(session["id"], "[oc-task:test] implement", agent="build")
        self.assertEqual(client.session_status("ses_new"), {"type": "busy"})
        self.assertEqual(client.session("ses_new")["directory"], str(directory))
        self.assertEqual(client.messages("ses_new")[0]["parts"][0]["text"], "done")
        self.assertEqual(client.session_diff("ses_new"), [{"file": "math_utils.py"}])
        self.assertTrue(client.abort("ses_new"))
        self.assertEqual(
            client.pending_permissions("ses_new"),
            [
                {
                    "id": "per_v2",
                    "sessionID": "ses_new",
                    "action": "read",
                    "resources": ["README.md"],
                }
            ],
        )
        self.assertEqual(
            client.pending_questions("ses_new"),
            [
                {
                    "id": "que_v2",
                    "sessionID": "ses_new",
                    "questions": [
                        {
                            "header": "Choice",
                            "question": "Continue?",
                            "options": [{"label": "Yes", "description": "Continue safely"}],
                        }
                    ],
                }
            ],
        )
        self.assertTrue(client.reply_permission("ses_new", "per_1", "once"))
        self.assertTrue(client.reply_question("que_1", [["Yes"]]))
        with client.event_response() as response:
            self.assertIn(b"server.connected", response.read())

        scoped = [
            request
            for request in ContractHandler.requests
            if request["path"].startswith("/session") or request["path"] == "/event"
        ]
        self.assertTrue(scoped)
        self.assertTrue(all(item["query"]["directory"] == [str(directory)] for item in scoped))
        prompt = next(item for item in ContractHandler.requests if item["path"].endswith("prompt_async"))
        self.assertEqual(
            prompt["body"],
            {
                "agent": "build",
                "parts": [{"type": "text", "text": "[oc-task:test] implement"}],
            },
        )
        permission = next(item for item in ContractHandler.requests if item["path"] == "/permission/per_1/reply")
        self.assertEqual(permission["body"], {"reply": "once"})
        question = next(item for item in ContractHandler.requests if item["path"] == "/question/que_1/reply")
        self.assertEqual(question["body"], {"answers": [["Yes"]]})

        permission_pending = next(
            item
            for item in ContractHandler.requests
            if item["path"] == "/api/session/ses_new/permission"
        )
        self.assertEqual(permission_pending["query"], {})
        question_pending = next(
            item
            for item in ContractHandler.requests
            if item["path"] == "/api/session/ses_new/question"
        )
        self.assertEqual(question_pending["query"], {})
        self.assertEqual(
            {
                item["path"]
                for item in ContractHandler.requests
                if item["path"] in {"/permission", "/question"}
            },
            {"/permission", "/question"},
        )

    def test_session_scoped_empty_list_is_reconciled_with_legacy_pending_input(self):
        class SplitPendingHandler(ContractHandler):
            def do_GET(self):
                parsed = self.record()
                if parsed.path == "/doc":
                    self.send_json(
                        {"paths": {"/api/session/{sessionID}/permission": {}}}
                    )
                elif parsed.path == "/api/session/ses_new/permission":
                    self.send_json({"data": []})
                elif parsed.path == "/permission":
                    self.send_json(
                        [
                            {
                                "id": "per_legacy",
                                "sessionID": "ses_new",
                                "permission": "external_directory",
                                "patterns": ["/tmp/opencode/*"],
                            }
                        ]
                    )
                else:
                    self.send_json({"error": "not found"}, status=404)

        server = ThreadingHTTPServer(("127.0.0.1", 0), SplitPendingHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = OpenCodeClient(
                f"http://127.0.0.1:{server.server_port}",
                Path("/tmp/split-worktree"),
            )

            self.assertEqual(
                client.pending_permissions("ses_new"),
                [
                    {
                        "id": "per_legacy",
                        "sessionID": "ses_new",
                        "permission": "external_directory",
                        "patterns": ["/tmp/opencode/*"],
                    }
                ],
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_session_scoped_pending_input_wins_when_legacy_duplicate_exists(self):
        class DuplicatePendingHandler(ContractHandler):
            def do_GET(self):
                parsed = self.record()
                if parsed.path == "/doc":
                    self.send_json(
                        {"paths": {"/api/session/{sessionID}/permission": {}}}
                    )
                elif parsed.path == "/api/session/ses_new/permission":
                    self.send_json(
                        {
                            "data": [
                                {
                                    "id": "per_same",
                                    "sessionID": "ses_new",
                                    "action": "read",
                                    "resources": ["README.md"],
                                }
                            ]
                        }
                    )
                elif parsed.path == "/permission":
                    self.send_json(
                        [
                            {
                                "id": "per_same",
                                "sessionID": "ses_new",
                                "permission": "write",
                                "patterns": ["README.md"],
                            }
                        ]
                    )
                else:
                    self.send_json({"error": "not found"}, status=404)

        server = ThreadingHTTPServer(("127.0.0.1", 0), DuplicatePendingHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = OpenCodeClient(
                f"http://127.0.0.1:{server.server_port}",
                Path("/tmp/split-worktree"),
            )

            self.assertEqual(
                client.pending_permissions("ses_new"),
                [
                    {
                        "id": "per_same",
                        "sessionID": "ses_new",
                        "action": "read",
                        "resources": ["README.md"],
                    }
                ],
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_invalid_legacy_pending_payload_fails_closed_when_v2_is_available(self):
        class InvalidSplitHandler(ContractHandler):
            def do_GET(self):
                parsed = self.record()
                if parsed.path == "/doc":
                    self.send_json(
                        {"paths": {"/api/session/{sessionID}/permission": {}}}
                    )
                elif parsed.path == "/api/session/ses_new/permission":
                    self.send_json({"data": []})
                elif parsed.path == "/permission":
                    self.send_json([{"id": "per_missing_session"}])
                else:
                    self.send_json({"error": "not found"}, status=404)

        server = ThreadingHTTPServer(("127.0.0.1", 0), InvalidSplitHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = OpenCodeClient(
                f"http://127.0.0.1:{server.server_port}",
                Path("/tmp/split-worktree"),
            )

            with self.assertRaisesRegex(OpenCodeError, "invalid pending-input list"):
                client.pending_permissions("ses_new")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_http_error_exposes_status_and_path_without_response_body(self):
        client = OpenCodeClient(self.base_url, Path("/tmp/worktree"))

        with self.assertRaises(OpenCodeError) as raised:
            client._request("GET", "/secret-error")

        error = raised.exception
        self.assertEqual(error.status, 502)
        self.assertEqual(error.path, "/secret-error")
        self.assertEqual(error.code, "http_error")
        self.assertNotIn("response-secret", str(error))
        self.assertNotIn("Authorization", str(error))

    def test_basic_auth_uses_environment_values_without_query_leak(self):
        client = OpenCodeClient(self.base_url, Path("/tmp/worktree"), username="agent", password="secret")

        client.health()

        request = ContractHandler.requests[-1]
        expected = base64.b64encode(b"agent:secret").decode()
        self.assertEqual(request["authorization"], f"Basic {expected}")
        self.assertNotIn("secret", str(request["query"]))

    def test_prompt_sends_selected_model_and_effort_variant(self):
        client = OpenCodeClient(self.base_url, Path("/tmp/worktree"))

        try:
            client.prompt_async(
                "ses_new",
                "implement",
                model={"providerID": "mcli", "modelID": "glm-5.3"},
                variant="max",
            )
        except TypeError as error:
            self.fail(f"prompt_async does not support model selection: {error}")

        prompt = ContractHandler.requests[-1]
        self.assertEqual(
            prompt["body"],
            {
                "agent": "build",
                "model": {"providerID": "mcli", "modelID": "glm-5.3"},
                "variant": "max",
                "parts": [{"type": "text", "text": "implement"}],
            },
        )

    def test_model_selection_rejects_an_unsupported_effort_with_available_values(self):
        client = OpenCodeClient(self.base_url, Path("/tmp/worktree"))

        try:
            validate = client.validate_model_selection
        except AttributeError as error:
            self.fail(f"client does not validate model selection: {error}")

        with self.assertRaisesRegex(
            RuntimeError,
            r"effort 'ultra'.*mcli/glm-5\.3.*fast, high, max",
        ):
            validate("mcli", "glm-5.3", "ultra")

    def test_remote_server_is_rejected_without_explicit_override(self):
        with self.assertRaisesRegex(ValueError, "non-loopback"):
            OpenCodeClient("http://example.com:4096", Path("/tmp/worktree"))

    def test_legacy_pending_lists_are_filtered_to_requested_session(self):
        class LegacyOnlyHandler(ContractHandler):
            def do_GET(self):
                parsed = self.record()
                if parsed.path == "/doc":
                    self.send_json({"paths": {"/permission/{requestID}/reply": {}}})
                elif parsed.path == "/permission":
                    self.send_json(
                        [
                            {
                                "id": "per_old",
                                "sessionID": "ses_old",
                                "permission": "read",
                                "patterns": ["old"],
                            },
                            {
                                "id": "per_new",
                                "sessionID": "ses_new",
                                "permission": "read",
                                "patterns": ["new"],
                            },
                        ]
                    )
                elif parsed.path == "/question":
                    self.send_json(
                        [
                            {"id": "que_old", "sessionID": "ses_old", "questions": []},
                            {"id": "que_new", "sessionID": "ses_new", "questions": []},
                        ]
                    )
                else:
                    self.send_json({"error": "not found"}, status=404)

        ContractHandler.requests = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), LegacyOnlyHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = OpenCodeClient(
                f"http://127.0.0.1:{server.server_port}",
                Path("/tmp/legacy-worktree"),
            )
            self.assertEqual(
                client.pending_permissions("ses_new"),
                [
                    {
                        "id": "per_new",
                        "sessionID": "ses_new",
                        "permission": "read",
                        "patterns": ["new"],
                    }
                ],
            )
            self.assertEqual(
                client.pending_questions("ses_new"),
                [{"id": "que_new", "sessionID": "ses_new", "questions": []}],
            )
            self.assertEqual(
                next(
                    item for item in ContractHandler.requests if item["path"] == "/permission"
                )["query"],
                {"directory": ["/tmp/legacy-worktree"]},
            )
            self.assertEqual(
                next(
                    item for item in ContractHandler.requests if item["path"] == "/question"
                )["query"],
                {"directory": ["/tmp/legacy-worktree"]},
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_invalid_legacy_pending_payload_raises_opencode_error(self):
        class InvalidLegacyHandler(ContractHandler):
            def do_GET(self):
                parsed = self.record()
                if parsed.path == "/doc":
                    self.send_json({"paths": {}})
                elif parsed.path == "/permission":
                    self.send_json({"data": []})
                else:
                    self.send_json({"error": "not found"}, status=404)

        server = ThreadingHTTPServer(("127.0.0.1", 0), InvalidLegacyHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = OpenCodeClient(
                f"http://127.0.0.1:{server.server_port}",
                Path("/tmp/legacy-worktree"),
            )
            with self.assertRaisesRegex(OpenCodeError, "invalid pending-input list"):
                client.pending_permissions("ses_new")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_session_scoped_pending_list_rejects_wrong_or_missing_ownership(self):
        class InvalidScopedHandler(ContractHandler):
            def do_GET(self):
                parsed = self.record()
                if parsed.path == "/doc":
                    self.send_json(
                        {"paths": {"/api/session/{sessionID}/permission": {}}}
                    )
                elif parsed.path == "/api/session/ses_new/permission":
                    self.send_json(
                        {
                            "data": [
                                {
                                    "id": "per-wrong-session",
                                    "sessionID": "ses_other",
                                    "action": "read",
                                    "resources": ["README.md"],
                                    "metadata": {"authorization": "Bearer scoped-secret"},
                                }
                            ]
                        }
                    )
                else:
                    self.send_json({"error": "not found"}, status=404)

        server = ThreadingHTTPServer(("127.0.0.1", 0), InvalidScopedHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = OpenCodeClient(
                f"http://127.0.0.1:{server.server_port}",
                Path("/tmp/scoped-worktree"),
            )

            with self.assertRaisesRegex(OpenCodeError, "invalid pending-input list") as raised:
                client.pending_permissions("ses_new")

            self.assertNotIn("scoped-secret", str(raised.exception))
            self.assertNotIn("ses_other", str(raised.exception))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_legacy_pending_lists_reject_invalid_session_ownership(self):
        malformed_items = (
            {"id": "per_missing"},
            {"id": "per_blank", "sessionID": ""},
            {"id": "per_non_string", "sessionID": 123},
        )

        for malformed_item in malformed_items:
            with self.subTest(malformed_item=malformed_item):
                class InvalidOwnershipHandler(ContractHandler):
                    def do_GET(self):
                        parsed = self.record()
                        if parsed.path == "/doc":
                            self.send_json({"paths": {}})
                        elif parsed.path in {"/permission", "/question"}:
                            self.send_json([malformed_item])
                        else:
                            self.send_json({"error": "not found"}, status=404)

                server = ThreadingHTTPServer(("127.0.0.1", 0), InvalidOwnershipHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    client = OpenCodeClient(
                        f"http://127.0.0.1:{server.server_port}",
                        Path("/tmp/legacy-worktree"),
                    )
                    with self.assertRaisesRegex(OpenCodeError, "invalid pending-input list"):
                        client.pending_permissions("ses_new")
                    with self.assertRaisesRegex(OpenCodeError, "invalid pending-input list"):
                        client.pending_questions("ses_new")
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
