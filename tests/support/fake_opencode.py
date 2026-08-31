from __future__ import annotations

from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import threading
import time
from urllib.parse import parse_qs, urlparse


@dataclass
class FakeSession:
    id: str
    title: str
    directory: str
    prompts: list[dict] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    busy: bool = False
    event_connections: int = 0
    model: dict | None = None
    pending_permissions: list[dict] = field(default_factory=list)
    pending_questions: list[dict] = field(default_factory=list)
    permission_replies: list[dict] = field(default_factory=list)
    question_replies: list[dict] = field(default_factory=list)


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        return


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    @property
    def fake(self) -> "FakeOpenCodeServer":
        return self.server.fake

    def parsed(self):
        return urlparse(self.path)

    def directory(self) -> str:
        return parse_qs(self.parsed().query).get("directory", [""])[0]

    def body(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        return json.loads(raw) if raw else None

    def send_json(self, payload, status=200):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_sse(self, payload):
        data = f"data: {json.dumps(payload)}\n\n".encode()
        self.wfile.write(data)
        self.wfile.flush()

    def do_GET(self):
        path = self.parsed().path
        if path == "/global/health":
            self.send_json({"healthy": True, "version": "fake-1.18.25"})
            return
        if path == "/doc":
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
            return
        if path.startswith("/api/session/") and path.endswith("/permission"):
            session_id = path.split("/")[3]
            session = self.fake.sessions.get(session_id)
            self.send_json({"data": list(session.pending_permissions) if session else []})
            return
        if path.startswith("/api/session/") and path.endswith("/question"):
            session_id = path.split("/")[3]
            session = self.fake.sessions.get(session_id)
            self.send_json({"data": list(session.pending_questions) if session else []})
            return
        if path == "/permission":
            session = self.fake.session_for_directory(self.directory())
            self.send_json(list(session.pending_permissions) if session else [])
            return
        if path == "/question":
            session = self.fake.session_for_directory(self.directory())
            self.send_json(list(session.pending_questions) if session else [])
            return
        if path == "/provider":
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
            return
        if path == "/session/status":
            with self.fake.condition:
                self.fake.status_request_count += 1
                session = self.fake.session_for_directory(self.directory())
                payload = {session.id: {"type": "busy"}} if session and session.busy else {}
            self.send_json(payload)
            return
        if path == "/event":
            self.handle_event()
            return
        if path.startswith("/session/") and path.endswith("/message"):
            session_id = path.split("/")[2]
            self.send_json(self.fake.sessions[session_id].messages)
            return
        if path.startswith("/session/") and path.count("/") == 2:
            session_id = path.split("/")[2]
            session = self.fake.sessions[session_id]
            payload = {"id": session.id, "title": session.title, "directory": session.directory}
            if session.model:
                payload["model"] = session.model
            self.send_json(payload)
            return
        if path.startswith("/session/") and path.endswith("/diff"):
            self.send_json([])
            return
        self.send_json({"error": "not found"}, status=404)

    def handle_event(self):
        session = self.fake.session_for_directory(self.directory())
        if session is None:
            self.send_json({"error": "session not found"}, status=404)
            return
        with self.fake.condition:
            session.event_connections += 1
            self.fake.event_connection_count += 1
            connection_number = session.event_connections
            baseline_activity = self.fake.activity
            session_was_busy = session.busy
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.send_sse({"type": "server.connected", "properties": {}})

        if self.fake.scenario == "disconnect_then_idle" and connection_number > 1:
            with self.fake.condition:
                session.busy = False
            self.fake.ensure_assistant(session)
            self.send_sse({"type": "session.idle", "properties": {"sessionID": session.id}})
            return

        deadline = time.monotonic() + 4
        with self.fake.condition:
            while (
                not session_was_busy
                and self.fake.activity == baseline_activity
                and time.monotonic() < deadline
            ):
                self.fake.condition.wait(timeout=0.1)

        if self.fake.scenario == "disconnect_then_idle":
            return
        if self.fake.scenario in {"delayed_idle", "blocking"}:
            with self.fake.condition:
                while not self.fake.release_idle.is_set() and session.busy:
                    self.fake.condition.wait(timeout=0.1)
                if not session.busy and not self.fake.release_idle.is_set():
                    return
                session.busy = False
            self.fake.ensure_assistant(session)
            self.send_sse({"type": "session.idle", "properties": {"sessionID": session.id}})
            return
        if self.fake.scenario == "permission":
            self.send_sse(
                {
                    "type": "permission.v2.asked",
                    "properties": {
                        "id": "per_1",
                        "sessionID": session.id,
                        "action": "unknown_capability",
                        "resources": ["README.md"],
                        "metadata": {"token": "hidden"},
                    },
                }
            )
            return
        if self.fake.scenario == "native_external_permission":
            if connection_number <= 2:
                self.send_sse(
                    {
                        "type": "permission.asked",
                        "properties": {
                            "id": "per_native_external",
                            "sessionID": session.id,
                            "action": "external_directory",
                            "resources": ["/external/reference/*"],
                            "metadata": {"token": "native-secret"},
                        },
                    }
                )
                return
            with self.fake.condition:
                session.busy = False
            self.fake.ensure_assistant(session)
            self.send_sse({"type": "session.idle", "properties": {"sessionID": session.id}})
            return
        if self.fake.scenario in {"queued_permissions", "queued_external_permissions"}:
            with self.fake.condition:
                pending = list(session.pending_permissions)
                if pending:
                    queue_deadline = time.monotonic() + 4
                    while (
                        self.fake.activity == baseline_activity
                        and time.monotonic() < queue_deadline
                    ):
                        self.fake.condition.wait(timeout=0.1)
                    pending = list(session.pending_permissions)
                if not pending:
                    session.busy = False
            if pending:
                first = pending[0]
                self.send_sse(
                    {
                        "type": "permission.v2.asked",
                        "properties": {
                            "id": first["id"],
                            "sessionID": session.id,
                            "action": first["action"],
                            "resources": list(first["resources"]),
                            "metadata": {"token": "hidden"},
                        },
                    }
                )
                return
            self.fake.ensure_assistant(session)
            self.send_sse({"type": "session.idle", "properties": {"sessionID": session.id}})
            return
        if self.fake.scenario == "heartbeat_only":
            for _ in range(200):
                try:
                    self.send_sse(
                        {
                            "type": "server.heartbeat",
                            "properties": {
                                "sessionID": session.id,
                                "metadata": {"secret": "heartbeat-secret"},
                            },
                        }
                    )
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
                time.sleep(0.05)
            return
        if self.fake.scenario == "question":
            self.send_sse(
                {
                    "type": "question.v2.asked",
                    "properties": {
                        "id": "que_1",
                        "sessionID": session.id,
                        "questions": [
                            {
                                "header": "Choice",
                                "question": "Continue?",
                                "options": [{"label": "Yes", "description": "Continue safely"}],
                            }
                        ],
                        "tool": {"secret": "hidden"},
                    },
                }
            )
            return
        if self.fake.scenario == "gated_question":
            with self.fake.condition:
                while not self.fake.release_question.is_set() and session.busy:
                    self.fake.condition.wait(timeout=0.1)
            self.send_sse(
                {
                    "type": "question.v2.asked",
                    "properties": {
                        "id": "que_1",
                        "sessionID": session.id,
                        "questions": [{"header": "Choice", "question": "Continue?"}],
                    },
                }
            )
            return
        if self.fake.scenario == "error":
            self.send_sse(
                {
                    "type": "session.error",
                    "properties": {
                        "sessionID": session.id,
                        "error": {"name": "FakeError", "message": "fake failure"},
                    },
                }
            )
            return

        with self.fake.condition:
            session.busy = False
        self.fake.ensure_assistant(session)
        self.send_sse({"type": "session.idle", "properties": {"sessionID": session.id}})

    def do_POST(self):
        path = self.parsed().path
        body = self.body()
        if path == "/session":
            session = self.fake.create_session(body["title"], self.directory())
            self.send_json({"id": session.id, "title": session.title, "directory": session.directory})
            return
        if path.startswith("/session/") and path.endswith("/prompt_async"):
            session_id = path.split("/")[2]
            session = self.fake.sessions[session_id]
            self.fake.record_prompt(session, body)
            if self.fake.scenario == "uncertain_post" and self.fake.prompt_count == 1:
                with self.fake.condition:
                    session.busy = False
                self.fake.ensure_assistant(session)
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.connection.close()
                return
            self.send_response(204)
            self.end_headers()
            return
        if path.startswith("/session/") and path.endswith("/abort"):
            session_id = path.split("/")[2]
            with self.fake.condition:
                self.fake.abort_count += 1
                self.fake.sessions[session_id].busy = False
                self.fake.activity += 1
                self.fake.condition.notify_all()
            self.send_json(True)
            return
        if path.startswith("/session/") and "/permissions/" in path:
            parts = path.split("/")
            session_id = parts[2]
            request_id = parts[4]
            session = self.fake.sessions[session_id]
            response = body.get("response") if isinstance(body, dict) else None
            self.fake.record_permission_reply(session, request_id, {"response": response})
            self.send_json(True)
            return
        if path.startswith("/permission/") or path.startswith("/question/"):
            with self.fake.condition:
                self.fake.activity += 1
                self.fake.condition.notify_all()
            if path.startswith("/permission/"):
                request_id = path.split("/")[2]
                session = self.fake.session_for_directory(self.directory())
                self.fake.record_permission_reply(session, request_id, body)
            else:
                request_id = path.split("/")[2]
                session = self.fake.session_for_directory(self.directory())
                self.fake.record_question_reply(session, request_id, body)
            self.send_json(True)
            return
        self.send_json({"error": "not found"}, status=404)


class FakeOpenCodeServer:
    def __init__(self, scenario: str = "idle"):
        self.scenario = scenario
        self.condition = threading.Condition()
        self.sessions: dict[str, FakeSession] = {}
        self.activity = 0
        self.prompt_count = 0
        self.abort_count = 0
        self.status_request_count = 0
        self.event_connection_count = 0
        self.created_session_count = 0
        self.session_directories: list[str] = []
        self.release_idle = threading.Event()
        self.release_question = threading.Event()
        self.server = QuietThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.fake = self
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> "FakeOpenCodeServer":
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release_idle.set()
        self.release_question.set()
        with self.condition:
            self.condition.notify_all()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def create_session(self, title: str, directory: str) -> FakeSession:
        with self.condition:
            self.created_session_count += 1
            session = FakeSession(
                id=f"ses_fake_{self.created_session_count}",
                title=title,
                directory=directory,
            )
            self.sessions[session.id] = session
            self.session_directories.append(directory)
            return session

    def session_for_directory(self, directory: str) -> FakeSession | None:
        matches = [session for session in self.sessions.values() if session.directory == directory]
        return matches[-1] if matches else None

    def record_prompt(self, session: FakeSession, body: dict) -> None:
        with self.condition:
            if body.get("model"):
                session.model = {
                    "id": body["model"]["modelID"],
                    "providerID": body["model"]["providerID"],
                    "variant": body.get("variant"),
                }
            session.prompts.append(body)
            session.messages.append(
                {
                    "info": {"role": "user"},
                    "parts": body["parts"],
                }
            )
            session.busy = True
            self.prompt_count += 1
            self.activity += 1
            if len(session.prompts) == 1 and self.scenario in {
                "queued_permissions",
                "queued_external_permissions",
            }:
                if self.scenario == "queued_permissions":
                    session.pending_permissions = [
                        {
                            "id": f"per_{index}",
                            "sessionID": session.id,
                            "action": "read",
                            "resources": ["README.md"],
                        }
                        for index in range(1, 4)
                    ]
                else:
                    session.pending_permissions = [
                        {
                            "id": f"per_{index}",
                            "sessionID": session.id,
                            "action": "external_directory",
                            "resources": [f"/old/worktree/{letter}.py"],
                        }
                        for index, letter in enumerate(("a", "b"), start=1)
                    ]
            if len(session.prompts) == 1 and self.scenario == "heartbeat_only":
                session.messages.append(
                    {
                        "info": {"role": "assistant", "createdAt": "2000-01-01T00:00:00+00:00"},
                        "parts": [
                            {
                                "id": "part-stall",
                                "type": "tool",
                                "callID": "call-stall",
                                "tool": "read",
                                "state": {
                                    "status": "running",
                                    "time": {"start": 946684800000},
                                    "input": {"secret": "hidden"},
                                },
                            }
                        ],
                    }
                )
            self.condition.notify_all()
        if self.scenario == "edit_idle" and len(session.prompts) == 1:
            worktree = Path(session.directory)
            (worktree / "math_utils.py").write_text(
                "def add(a: int, b: int) -> int:\n"
                "    return a + b\n\n\n"
                "def multiply(a: int, b: int) -> int:\n"
                "    return a * b\n",
                encoding="utf-8",
            )
            (worktree / "tests/test_math_utils.py").write_text(
                "import unittest\n"
                "from math_utils import add, multiply\n\n\n"
                "class MathUtilsTest(unittest.TestCase):\n"
                "    def test_add(self):\n"
                "        self.assertEqual(add(2, 3), 5)\n\n"
                "    def test_multiply(self):\n"
                "        self.assertEqual(multiply(6, 7), 42)\n",
                encoding="utf-8",
            )

    def ensure_assistant(self, session: FakeSession) -> None:
        assistants = [
            message
            for message in session.messages
            if (message.get("info") or {}).get("role") == "assistant"
        ]
        if len(assistants) >= len(session.prompts):
            return
        text = (
            "OPENCODE_REVIEW_ACK [oc-task:fake] "
            "python3 -m unittest discover -s tests -t . -v"
            if len(session.prompts) > 1
            else "FAKE_DONE"
        )
        session.messages.append(
            {
                "info": {"role": "assistant"},
                "parts": [{"type": "text", "text": text}],
            }
        )

    def record_permission_reply(
        self, session: FakeSession | None, request_id: str, body: dict
    ) -> None:
        if session is None:
            return
        with self.condition:
            session.permission_replies.append(
                {"request_id": request_id, "body": dict(body or {})}
            )
            session.pending_permissions = [
                item for item in session.pending_permissions if item.get("id") != request_id
            ]
            self.activity += 1
            self.condition.notify_all()

    def record_question_reply(
        self, session: FakeSession | None, request_id: str, body: dict
    ) -> None:
        if session is None:
            return
        with self.condition:
            session.question_replies.append(
                {"request_id": request_id, "body": dict(body or {})}
            )
            session.pending_questions = [
                item for item in session.pending_questions if item.get("id") != request_id
            ]
            self.activity += 1
            self.condition.notify_all()
