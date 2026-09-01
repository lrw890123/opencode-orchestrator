from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest

from opencode_orchestrator.service import BridgeService
from opencode_orchestrator.opencode_client import OpenCodeError
from opencode_orchestrator.permission_policy import (
    normalize_permission_policy,
    normalize_progress_policy,
)
from opencode_orchestrator.task_state import Phase
from tests.test_git_workspace import create_repo


LOW_REQUEST = {
    "goal": "Update the demo",
    "non_goals": [],
    "approved_plan": ["Inspect README", "Make the requested edit"],
    "allowed_paths": ["README.md"],
    "forbidden_actions": ["Do not touch other files"],
    "acceptance_criteria": ["README remains valid text"],
    "test_commands": [],
    "risk": {
        "file_count": 1,
        "line_count": 5,
        "cross_module": False,
        "public_interface": False,
        "dependency_change": False,
        "high_risk_actions": [],
    },
    "user_approved": False,
}


def event(payload: dict) -> list[bytes]:
    return [f"data: {json.dumps(payload)}\n".encode(), b"\n"]


class FakeResponse:
    def __init__(self, lines):
        self.lines = lines

    def __iter__(self):
        return iter(self.lines)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None


class BlockingResponse:
    def __init__(self):
        self.released = threading.Event()

    def __iter__(self):
        yield from event({"type": "server.connected", "properties": {}})
        self.released.wait(timeout=5)

    def close(self):
        self.released.set()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return None


class FakeClient:
    def __init__(self):
        self.directory = None
        self.scenario = "idle"
        self.created_session_count = 0
        self.prompt_count = 0
        self.session_id = "ses_fake_new"
        self.aborted = False
        self.abort_count = 0
        self.prompt_calls = []
        self.prompted = threading.Event()
        self._pending_permissions = []
        self._pending_questions = []
        self.permission_replies = []
        self.question_replies = []
        self.pending_permissions_calls = 0
        self.pending_questions_calls = 0
        self.native_permission_event_count = 0
        self.message_payload = [
            {
                "info": {"role": "assistant"},
                "parts": [{"type": "text", "text": "FAKE_DONE"}],
            }
        ]

    def health(self):
        return {"healthy": True, "version": "fake-1"}

    def create_session(self, title):
        self.created_session_count += 1
        return {"id": self.session_id, "title": title}

    def event_response(self):
        lines = event({"type": "server.connected", "properties": {}})
        if self.scenario in {"idle", "idle_busy"}:
            lines += event({"type": "session.idle", "properties": {"sessionID": self.session_id}})
        elif self.scenario == "permission":
            lines += event(
                {
                    "type": "permission.v2.asked",
                    "properties": {
                        "id": "per_1",
                        "sessionID": self.session_id,
                        "action": "unknown_capability",
                        "resources": ["README.md"],
                    },
                }
            )
        elif self.scenario == "native_external_permission":
            self.native_permission_event_count += 1
            if self.native_permission_event_count <= 2:
                lines += event(
                    {
                        "type": "permission.asked",
                        "properties": {
                            "id": "per_native_external",
                            "sessionID": self.session_id,
                            "action": "external_directory",
                            "resources": ["/external/reference/*"],
                            "metadata": {"token": "native-secret"},
                        },
                    }
                )
            else:
                lines += event(
                    {
                        "type": "session.idle",
                        "properties": {"sessionID": self.session_id},
                    }
                )
        return FakeResponse(lines)

    def validate_model_selection(self, provider_id, model_id, effort):
        return {
            "providerID": provider_id,
            "modelID": model_id,
            "variant": effort,
        }

    def prompt_async(self, session_id, text, agent="build", model=None, variant=None):
        self.prompt_count += 1
        self.last_prompt = {
            "session_id": session_id,
            "text": text,
            "agent": agent,
            "model": model,
            "variant": variant,
        }
        self.prompt_calls.append(self.last_prompt)
        self.prompted.set()

    def messages(self, session_id, limit=100):
        return self.message_payload[:limit]

    def session_diff(self, session_id):
        return []

    def session_status(self, session_id):
        return {"type": "busy"} if self.scenario == "idle_busy" else None

    def abort(self, session_id):
        self.aborted = True
        self.abort_count += 1
        return True

    def reply_permission(self, session_id, request_id, response):
        self.permission_replies.append((session_id, request_id, response))
        self._pending_permissions = [
            item for item in self._pending_permissions if item.get("id") != request_id
        ]
        return True

    def reply_question(self, request_id, answers):
        self.question_replies.append((request_id, answers))
        self._pending_questions = [
            item for item in self._pending_questions if item.get("id") != request_id
        ]
        return True

    def pending_permissions(self, session_id):
        self.pending_permissions_calls += 1
        return [
            item for item in self._pending_permissions
            if item.get("sessionID") == session_id
        ]

    def pending_questions(self, session_id):
        self.pending_questions_calls += 1
        return [
            item for item in self._pending_questions
            if item.get("sessionID") == session_id
        ]


class NativeP2WithApiP1Client(FakeClient):
    def __init__(self):
        super().__init__()
        self.responses = 0

    def event_response(self):
        self.responses += 1
        lines = event({"type": "server.connected", "properties": {}})
        if self.responses == 1:
            lines += event(
                {
                    "type": "permission.asked",
                    "properties": {
                        "id": "per-native-p2",
                        "sessionID": self.session_id,
                        "action": "external_directory",
                        "resources": ["/undeclared/p2.txt"],
                        "metadata": {"authorization": "Bearer native-p2-secret"},
                    },
                }
            )
        else:
            lines += event(
                {"type": "session.idle", "properties": {"sessionID": self.session_id}}
            )
        return FakeResponse(lines)


class BlockingClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.response = BlockingResponse()

    def event_response(self):
        return self.response


class ConnectFailOnceClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.connection_attempts = 0

    def event_response(self):
        self.connection_attempts += 1
        if self.connection_attempts == 1:
            raise OpenCodeError("fake event connection failure")
        return super().event_response()


class AcceptedPromptUncertainClient(FakeClient):
    """Simulate an accepted prompt followed by unrelated session history."""

    def __init__(self):
        super().__init__()
        self.prompt_attempts = 0

    def prompt_async(self, *args, **kwargs):
        self.prompt_attempts += 1
        super().prompt_async(*args, **kwargs)
        if self.prompt_attempts == 1:
            raise OpenCodeError(
                "OpenCode HTTP 503 for /session/ses_fake_new/prompt_async: "
                "accepted-but-lost secret=prompt-secret"
            )

    def messages(self, session_id, limit=100):
        return [
            {
                "info": {"role": "assistant"},
                "parts": [{"type": "text", "text": "Unrelated earlier session output"}],
            }
        ]


class ContinueOrderingClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.listener_opened = False

    def event_response(self):
        self.listener_opened = True
        return super().event_response()

    def prompt_async(self, *args, **kwargs):
        if not self.listener_opened:
            raise AssertionError("continuation prompt was sent before SSE listening")
        return super().prompt_async(*args, **kwargs)


class ExternalResumeClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.external_busy = False

    def session_status(self, session_id):
        return {"type": "busy"} if self.external_busy else None

    def event_response(self):
        lines = event({"type": "server.connected", "properties": {}})
        self.external_busy = False
        lines += event(
            {"type": "session.idle", "properties": {"sessionID": self.session_id}}
        )
        return FakeResponse(lines)


class LateExternalActivityClient(FakeClient):
    """Terminal session gains external activity after resume_wait blocks."""

    def __init__(self, *, finishes: bool):
        super().__init__()
        self.finishes = finishes
        self.armed = False
        self.activity_seen = False
        self.external_busy = False
        activity_at = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        self.external_messages = [
            {
                "info": {"role": "user", "createdAt": activity_at},
                "parts": [{"type": "text", "text": "EXTERNAL_USER_TURN"}],
            },
            {
                "info": {"role": "assistant", "createdAt": activity_at},
                "parts": [{"type": "text", "text": "EXTERNAL_ASSISTANT_TURN"}],
            },
        ]

    def session_status(self, session_id):
        return {"type": "busy"} if self.external_busy else None

    def event_response(self):
        lines = event({"type": "server.connected", "properties": {}})
        if self.armed and not self.activity_seen:
            self.activity_seen = True
            self.external_busy = True
            self.message_payload = self.external_messages
            lines += event(
                {
                    "type": "message.updated",
                    "properties": {"sessionID": self.session_id},
                }
            )
            if self.finishes:
                self.external_busy = False
                lines += event(
                    {
                        "type": "session.idle",
                        "properties": {"sessionID": self.session_id},
                    }
                )
            return FakeResponse(lines)
        if self.activity_seen and self.finishes:
            self.external_busy = False
            lines += event(
                {"type": "session.idle", "properties": {"sessionID": self.session_id}}
            )
            return FakeResponse(lines)
        lines += event(
            {"type": "session.idle", "properties": {"sessionID": self.session_id}}
        )
        return FakeResponse(lines)


class AcceptedContinuationUncertainClient(ContinueOrderingClient):
    def prompt_async(self, session_id, text, **kwargs):
        super().prompt_async(session_id, text, **kwargs)
        self.message_payload = [
            {
                "info": {"role": "user"},
                "parts": [{"type": "text", "text": text}],
            },
            {
                "info": {"role": "assistant"},
                "parts": [{"type": "text", "text": "Continuation completed"}],
            },
        ]
        raise OpenCodeError("accepted continuation response was lost")


class V2DiscoveryErrorClient(FakeClient):
    def openapi_paths(self):
        return {
            "/api/session/{sessionID}/permission",
            "/api/session/{sessionID}/question",
        }

    def pending_permissions(self, session_id):
        self.pending_permissions_calls += 1
        raise OpenCodeError(
            f"OpenCode HTTP 404 for /api/session/{session_id}/permission: "
            "not found secret=discovery-secret"
        )


class LegacyDiscoveryAbsentClient(FakeClient):
    def openapi_paths(self):
        return set()

    def pending_permissions(self, session_id):
        self.pending_permissions_calls += 1
        raise OpenCodeError("OpenCode HTTP 404 for /permission: unsupported")

    def pending_questions(self, session_id):
        self.pending_questions_calls += 1
        raise OpenCodeError("OpenCode HTTP 404 for /question: unsupported")


class ReplyErrorClient(FakeClient):
    def reply_permission(self, session_id, request_id, response):
        raise OpenCodeError(
            "OpenCode HTTP 500 for /permission/reply: reply-secret Authorization=Bearer secret"
        )


class AbortErrorClient(FakeClient):
    def abort(self, session_id):
        self.abort_count += 1
        raise OpenCodeError(
            "OpenCode HTTP 503 for /session/ses_fake_new/abort: "
            "abort-secret Authorization=Bearer secret"
        )


class HeartbeatBurstClient(FakeClient):
    def event_response(self):
        lines = event({"type": "server.connected", "properties": {}})
        for _ in range(50):
            lines += event(
                {
                    "type": "server.heartbeat",
                    "properties": {"sessionID": self.session_id},
                }
            )
        return FakeResponse(lines)

    def session_status(self, session_id):
        return {"type": "busy"}


class HeartbeatThenIdleClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.event_response_count = 0

    def event_response(self):
        self.event_response_count += 1
        lines = event({"type": "server.connected", "properties": {}})
        if self.event_response_count == 1:
            lines += event(
                {
                    "type": "server.heartbeat",
                    "properties": {
                        "sessionID": self.session_id,
                        "secret": "heartbeat-secret",
                    },
                }
            )
            return FakeResponse(lines)
        lines += event(
            {"type": "session.idle", "properties": {"sessionID": self.session_id}}
        )
        return FakeResponse(lines)

    def session_status(self, session_id):
        return {"type": "busy"} if self.event_response_count == 1 else None


class ClientFactory:
    def __init__(self, client):
        self.client = client
        self.calls = []

    def __call__(self, base_url, directory, **kwargs):
        self.client.directory = Path(directory)
        self.calls.append((base_url, Path(directory), kwargs))
        return self.client


class BridgeServiceTest(unittest.TestCase):
    def make_service(self, root, client):
        return BridgeService(Path(root) / "state", client_factory=ClientFactory(client))

    def _prime_paused_task(self, service, client, source, request=None):
        prepared = service.prepare_task(
            source,
            "paused-continuation",
            deepcopy(request or LOW_REQUEST),
        )
        service._ensure_session(
            prepared["task_id"],
            client,
            service._request(prepared["task_id"]),
        )

        def prime(current):
            current["execution_state"] = "RUNNING"
            current["phase"] = Phase.PAUSED
            current["opencode"]["dispatch_state"] = "SENT"

        service.store.update(prepared["task_id"], prime)
        return prepared

    def _prime_reviewing_task(self, service, source, request=None):
        prepared = service.prepare_task(
            source,
            "reviewing-external-resume",
            deepcopy(request or LOW_REQUEST),
        )
        service.dispatch(prepared["task_id"], timeout_seconds=2)
        service.collect_result(prepared["task_id"])
        return prepared

    def test_continue_sends_after_listener_and_reuses_model_effort_and_session(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = ContinueOrderingClient()
            request = deepcopy(LOW_REQUEST)
            request["model"] = {"providerID": "mcli", "modelID": "glm-5.3"}
            request["effort"] = "max"
            service = self.make_service(tmp, client)
            prepared = self._prime_paused_task(service, client, source, request)

            result = service.reply(
                prepared["task_id"],
                "continue",
                {"text": "Continue within the existing approved scope."},
                timeout_seconds=2,
            )
            state = service.store.load(prepared["task_id"])

            self.assertEqual(result["outcome"], "idle")
            self.assertEqual(client.created_session_count, 1)
            self.assertEqual(client.prompt_count, 1)
            self.assertEqual(client.last_prompt["session_id"], "ses_fake_new")
            self.assertEqual(
                client.last_prompt["model"],
                {"providerID": "mcli", "modelID": "glm-5.3"},
            )
            self.assertEqual(client.last_prompt["variant"], "max")
            self.assertIn(
                f"[oc-task:{prepared['task_id']}] continuation 1",
                client.last_prompt["text"],
            )
            self.assertEqual(state["execution"]["continuation_round"], 1)
            self.assertEqual(
                state["execution"]["continuation"]["dispatch_state"],
                "SENT",
            )

    def test_continue_rejects_busy_or_pending_session_without_sending(self):
        cases = ("busy", "permission", "question", "tool")
        for case in cases:
            with self.subTest(case=case), TemporaryDirectory() as tmp:
                source = create_repo(Path(tmp) / "source")
                client = ContinueOrderingClient()
                if case == "busy":
                    client.scenario = "idle_busy"
                elif case == "permission":
                    client._pending_permissions = [
                        {
                            "id": "per-continue",
                            "sessionID": client.session_id,
                            "action": "read",
                            "resources": ["README.md"],
                        }
                    ]
                elif case == "question":
                    client._pending_questions = [
                        {
                            "id": "que-continue",
                            "sessionID": client.session_id,
                            "questions": [
                                {"header": "Choice", "question": "Continue?"}
                            ],
                        }
                    ]
                else:
                    client.message_payload = [
                        {
                            "info": {"role": "assistant"},
                            "parts": [
                                {
                                    "id": "part-continue",
                                    "type": "tool",
                                    "callID": "call-continue",
                                    "tool": "bash",
                                    "state": {
                                        "status": "running",
                                        "time": {"start": 1788045720839},
                                    },
                                }
                            ],
                        }
                    ]
                service = self.make_service(tmp, client)
                prepared = self._prime_paused_task(service, client, source)

                with self.assertRaisesRegex(ValueError, "cannot continue"):
                    service.reply(
                        prepared["task_id"],
                        "continue",
                        {"text": "Continue within scope."},
                        timeout_seconds=2,
                    )

                self.assertEqual(client.prompt_count, 0)

    def test_continue_reacquires_aborted_task_in_original_session(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = ContinueOrderingClient()
            service = self.make_service(tmp, client)
            prepared = self._prime_reviewing_task(service, source)
            service.abort_task(prepared["task_id"])

            result = service.reply(
                prepared["task_id"],
                "continue",
                {
                    "text": "Resume the remaining approved scope.",
                    "reacquire": True,
                },
                timeout_seconds=2,
            )
            state = service.store.load(prepared["task_id"])

            self.assertEqual(result["outcome"], "idle")
            self.assertEqual(client.created_session_count, 1)
            self.assertEqual(client.prompt_count, 2)
            self.assertEqual(client.last_prompt["session_id"], "ses_fake_new")
            self.assertIn(
                f"[oc-task:{prepared['task_id']}] continuation 1",
                client.last_prompt["text"],
            )
            self.assertEqual(
                state["execution"]["continuation"]["dispatch_state"],
                "SENT",
            )
            self.assertEqual(state["execution"]["external_reentry_count"], 1)
            self.assertEqual(state["abort"]["state"], "SUPERSEDED")
            self.assertEqual(
                state["abort"]["superseded_by"],
                "external-session-activity",
            )
            self.assertEqual(state["execution_state"], "COMPLETED")
            self.assertEqual(state["phase"], Phase.COLLECTING)
            self.assertEqual(client.abort_count, 1)

    def test_continue_reacquires_completed_task_and_invalidates_review(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = ContinueOrderingClient()
            service = self.make_service(tmp, client)
            prepared = self._prime_reviewing_task(service, source)
            self.assertEqual(
                service.store.load(prepared["task_id"])["review_state"],
                "REVIEWING",
            )

            result = service.reply(
                prepared["task_id"],
                "continue",
                {
                    "text": "Address the review finding in the same scope.",
                    "reacquire": True,
                },
                timeout_seconds=2,
            )
            state = service.store.load(prepared["task_id"])

            self.assertEqual(result["outcome"], "idle")
            self.assertEqual(client.created_session_count, 1)
            self.assertEqual(client.prompt_count, 2)
            self.assertIsNotNone(state["execution"].get("review_invalidated_at"))
            self.assertEqual(state["execution"]["external_reentry_count"], 1)
            self.assertEqual(state["execution_state"], "COMPLETED")
            self.assertEqual(state["phase"], Phase.COLLECTING)

    def test_continue_without_reacquire_rejects_terminal_task(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = ContinueOrderingClient()
            service = self.make_service(tmp, client)
            prepared = self._prime_reviewing_task(service, source)
            service.abort_task(prepared["task_id"])

            with self.assertRaisesRegex(
                ValueError,
                "reacquire=true",
            ):
                service.reply(
                    prepared["task_id"],
                    "continue",
                    {"text": "Resume the remaining scope."},
                    timeout_seconds=2,
                )

            state = service.store.load(prepared["task_id"])
            self.assertEqual(state["execution_state"], "ABORTED")
            self.assertEqual(state["abort"]["state"], "COMPLETED")
            self.assertEqual(client.prompt_count, 1)

    def test_continue_reacquire_rejects_abort_still_in_progress(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = ContinueOrderingClient()
            service = self.make_service(tmp, client)
            prepared = self._prime_reviewing_task(service, source)
            service.abort_task(prepared["task_id"])

            def mark_requested(current):
                current["abort"] = dict(current.get("abort") or {})
                current["abort"]["state"] = "REQUESTED"

            service.store.update(prepared["task_id"], mark_requested)

            with self.assertRaisesRegex(
                ValueError,
                "abort request is still in progress",
            ):
                service.reply(
                    prepared["task_id"],
                    "continue",
                    {
                        "text": "Resume the remaining scope.",
                        "reacquire": True,
                    },
                    timeout_seconds=2,
                )

            self.assertEqual(client.prompt_count, 1)

    def test_continue_reacquire_rejects_busy_terminal_session(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = ContinueOrderingClient()
            service = self.make_service(tmp, client)
            prepared = self._prime_reviewing_task(service, source)
            service.abort_task(prepared["task_id"])
            client.scenario = "idle_busy"

            with self.assertRaisesRegex(
                ValueError,
                "still busy; use resume_wait",
            ):
                service.reply(
                    prepared["task_id"],
                    "continue",
                    {
                        "text": "Resume the remaining scope.",
                        "reacquire": True,
                    },
                    timeout_seconds=2,
                )

            state = service.store.load(prepared["task_id"])
            self.assertEqual(state["execution_state"], "ABORTED")
            self.assertEqual(client.prompt_count, 1)

    def test_accepted_uncertain_continuation_is_recovered_without_resend(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = AcceptedContinuationUncertainClient()
            service = self.make_service(tmp, client)
            prepared = self._prime_paused_task(service, client, source)

            first = service.reply(
                prepared["task_id"],
                "continue",
                {"text": "Continue within scope."},
                timeout_seconds=2,
            )
            resumed = service.wait(prepared["task_id"], timeout_seconds=2)
            state = service.store.load(prepared["task_id"])

            self.assertEqual(first["outcome"], "disconnected")
            self.assertEqual(resumed["outcome"], "idle")
            self.assertEqual(client.prompt_count, 1)
            self.assertEqual(
                state["execution"]["continuation"]["dispatch_state"],
                "SENT",
            )
            self.assertEqual(
                state["execution"]["continuation"]["recovered_from"],
                "message-history",
            )

    def test_dispatch_wait_does_not_hold_task_lock_and_cancel_does_not_abort(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = BlockingClient()
            service = self.make_service(tmp, client)
            prepared = service.prepare_task(
                source,
                "blocking",
                deepcopy(LOW_REQUEST),
                "http://127.0.0.1:4096",
            )
            task_id = prepared["task_id"]
            result = {}
            errors = []

            def run_wait():
                try:
                    with service.wait_coordinator.attach(task_id, "request-block") as lease:
                        result.update(service.dispatch_and_wait(task_id, 10, lease))
                except Exception as error:
                    errors.append(error)

            thread = threading.Thread(target=run_wait)
            thread.start()
            self.assertTrue(client.prompted.wait(timeout=2), "dispatch never reached OpenCode")

            started = time.monotonic()
            status = service.status(task_id)
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 0.5)
            self.assertEqual(status["execution_state"], "RUNNING")
            self.assertTrue(service.wait_coordinator.cancel_task(task_id, "client-cancelled"))
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(result["outcome"], "WAIT_CANCELLED")
            self.assertEqual(service.status(task_id)["execution_state"], "RUNNING")
            self.assertEqual(client.abort_count, 0)

    def test_idle_event_while_session_is_busy_is_interrupted(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = FakeClient()
            client.scenario = "idle_busy"
            service = self.make_service(tmp, client)
            prepared = service.prepare_task(
                source,
                "busy-idle",
                deepcopy(LOW_REQUEST),
                "http://127.0.0.1:4096",
            )

            with service.wait_coordinator.attach(prepared["task_id"], "request-busy") as lease:
                result = service.dispatch_and_wait(prepared["task_id"], 2, lease)

            self.assertEqual(result["outcome"], "INTERRUPTED")
            self.assertEqual(result["reason"], "idle-not-reconciled")
            self.assertEqual(result["execution_state"], "RUNNING")

    def test_resume_sends_initial_prompt_after_pre_send_connection_failure(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = ConnectFailOnceClient()
            service = self.make_service(tmp, client)
            prepared = service.prepare_task(source, "retry-connect", deepcopy(LOW_REQUEST))

            first = service.dispatch(prepared["task_id"], timeout_seconds=2)
            resumed = service.wait(prepared["task_id"], timeout_seconds=2)

            self.assertEqual(first["outcome"], "disconnected")
            self.assertEqual(resumed["outcome"], "idle")
            self.assertEqual(client.prompt_count, 1)
            self.assertEqual(client.created_session_count, 1)

    def test_progress_observation_accumulates_counters_and_excludes_transport_logs(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = HeartbeatThenIdleClient()
            service = self.make_service(tmp, client)
            prepared = service.prepare(source, "progress", deepcopy(LOW_REQUEST))

            first = service.dispatch(prepared["task_id"], timeout_seconds=2)
            resumed = service.wait(prepared["task_id"], timeout_seconds=2)
            state = service.status(prepared["task_id"])
            event_log = (
                service.store.task_dir(prepared["task_id"]) / "events.jsonl"
            ).read_text(encoding="utf-8")

            self.assertEqual(first["outcome"], "disconnected")
            self.assertEqual(resumed["outcome"], "idle")
            self.assertEqual(
                state["execution"]["event_counts"],
                {
                    "server.connected": 2,
                    "server.heartbeat": 1,
                    "session.idle": 1,
                },
            )
            self.assertEqual(resumed["counters"], state["execution"]["event_counts"])
            self.assertEqual(state["progress"]["heartbeat_count"], 1)
            self.assertEqual(state["progress"]["last_progress_event"], "session.idle")
            self.assertNotIn("server.connected", event_log)
            self.assertNotIn("heartbeat-secret", event_log)

    def test_prepare_persists_a_stable_task_fingerprint(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            service = self.make_service(tmp, FakeClient())

            first = service.prepare_task(source, "one", deepcopy(LOW_REQUEST))
            second = service.prepare_task(source, "two", deepcopy(LOW_REQUEST))

            self.assertRegex(first["task_fingerprint"], r"^sha256:[0-9a-f]{64}$")
            self.assertEqual(first["task_fingerprint"], second["task_fingerprint"])

    def test_prepare_persists_normalized_policies_in_request_fingerprint_and_state(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            permission_input = {
                "default": "ask",
                "rules": [
                    {
                        "permission": "external_directory",
                        "pattern": "/refs/./old-tree/src/../src/**",
                        "action": "allow",
                    }
                ],
            }
            progress_input = {
                "input_probe_interval_seconds": 20,
                "stall_timeout_seconds": 900,
            }
            request = deepcopy(LOW_REQUEST)
            request["permission_policy"] = permission_input
            request["progress_policy"] = progress_input
            service = self.make_service(tmp, FakeClient())

            prepared = service.prepare_task(source, "policy-contract", request)
            expected_permission = normalize_permission_policy(permission_input)
            expected_progress = normalize_progress_policy(progress_input)
            request_path = service.store.task_dir(prepared["task_id"]) / "request.json"
            persisted_request = json.loads(request_path.read_text(encoding="utf-8"))
            state = service.status(prepared["task_id"])

            self.assertEqual(persisted_request["permission_policy"], expected_permission)
            self.assertEqual(persisted_request["progress_policy"], expected_progress)
            self.assertEqual(state["permission_policy"], expected_permission)
            self.assertEqual(state["progress_policy"], expected_progress)
            self.assertEqual(
                state["task_fingerprint"],
                service._task_fingerprint(state["source"]["base_sha"], persisted_request),
            )
            self.assertNotEqual(
                state["task_fingerprint"],
                service._task_fingerprint(state["source"]["base_sha"], request),
            )

    def test_transcript_is_paginated_and_tool_output_is_opt_in(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = FakeClient()
            client.message_payload = [
                {
                    "info": {"role": "user", "time": {"created": 1}},
                    "parts": [{"type": "text", "text": "Please inspect"}],
                },
                {
                    "info": {"role": "assistant", "time": {"created": 2}},
                    "parts": [
                        {"type": "reasoning", "text": "private chain"},
                        {
                            "type": "tool",
                            "tool": "bash",
                            "state": {"status": "completed", "output": "SECRET_OUTPUT"},
                        },
                        {"type": "text", "text": "Done"},
                    ],
                },
            ]
            service = self.make_service(tmp, client)
            prepared = service.prepare_task(source, "transcript", deepcopy(LOW_REQUEST))
            service.dispatch(prepared["task_id"], timeout_seconds=2)

            first = service.read_transcript(prepared["task_id"], None, 1, False)
            second = service.read_transcript(prepared["task_id"], first["next_cursor"], 100, False)
            with_output = service.read_transcript(prepared["task_id"], "1", 100, True)

            self.assertEqual(first["next_cursor"], "1")
            self.assertEqual(first["messages"][0]["index"], 0)
            self.assertIsNone(second["next_cursor"])
            self.assertNotIn("private chain", str(second))
            self.assertNotIn("SECRET_OUTPUT", str(second))
            self.assertIn("SECRET_OUTPUT", str(with_output))
            cached = (service.store.task_dir(prepared["task_id"]) / "transcript.json").read_text()
            self.assertNotIn("SECRET_OUTPUT", cached)

    def test_prepare_dispatch_collect_and_review_reuse_one_session(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = FakeClient()
            service = self.make_service(tmp, client)

            prepared = service.prepare(source, "demo", deepcopy(LOW_REQUEST), "http://127.0.0.1:4096")
            dispatched = service.dispatch(prepared["task_id"], timeout_seconds=2)
            result = service.collect(prepared["task_id"])
            review = service.reply(
                prepared["task_id"],
                "review",
                {"text": "Read-only recheck"},
                timeout_seconds=2,
            )

            self.assertEqual(prepared["phase"], Phase.PREPARING)
            self.assertEqual(dispatched["outcome"], "idle")
            self.assertEqual(result["assistant_result"], "FAKE_DONE")
            self.assertEqual(result["phase"], Phase.REVIEWING)
            self.assertEqual(review["outcome"], "idle")
            self.assertEqual(client.created_session_count, 1)
            self.assertEqual(client.prompt_count, 2)
            self.assertEqual(client.last_prompt["session_id"], "ses_fake_new")
            self.assertEqual(service.status(prepared["task_id"])["phase"], Phase.COLLECTING)

    def test_status_projects_external_permission_without_mutating_completed_state(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = FakeClient()
            service = self.make_service(tmp, client)
            prepared = self._prime_reviewing_task(service, source)
            client.scenario = "idle_busy"
            client._pending_permissions = [
                {
                    "id": "per-external-turn",
                    "sessionID": client.session_id,
                    "action": "external_directory",
                    "resources": ["/tmp/*"],
                    "source": {"callID": "call-external-turn"},
                }
            ]

            projected = service.status(prepared["task_id"])
            persisted = service.store.load(prepared["task_id"])

            self.assertEqual(projected["execution_state"], "INPUT_REQUIRED")
            self.assertEqual(projected["phase"], Phase.PERMISSION_WAIT)
            self.assertEqual(projected["review_state"], "REVISION_REQUESTED")
            self.assertTrue(projected["progress"]["external_activity_detected"])
            self.assertEqual(
                projected["progress"]["pending_permissions"][0]["request_id"],
                "per-external-turn",
            )
            self.assertEqual(persisted["execution_state"], "COMPLETED")
            self.assertEqual(persisted["phase"], Phase.REVIEWING)

    def test_completed_task_can_reply_to_live_permission_and_remember_exact_task_rule(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = FakeClient()
            service = self.make_service(tmp, client)
            prepared = self._prime_reviewing_task(service, source)
            client._pending_permissions = [
                {
                    "id": "per-external-turn",
                    "sessionID": client.session_id,
                    "action": "external_directory",
                    "resources": ["/tmp/*"],
                }
            ]

            result = service.reply(
                prepared["task_id"],
                "permission",
                {
                    "request_id": "per-external-turn",
                    "response": "once",
                    "user_approved": True,
                    "approval_basis": "Approve external_directory /tmp/* for this task.",
                    "remember_for_task": True,
                },
                timeout_seconds=2,
            )
            state = service.store.load(prepared["task_id"])

            self.assertEqual(result["outcome"], "idle")
            self.assertEqual(result["phase"], Phase.COLLECTING)
            self.assertEqual(result["session_id"], client.session_id)
            self.assertEqual(client.created_session_count, 1)
            self.assertEqual(client.prompt_count, 1)
            self.assertEqual(
                client.permission_replies,
                [(client.session_id, "per-external-turn", "once")],
            )
            self.assertEqual(
                state["task_permission_rules"],
                [
                    {
                        "permission": "external_directory",
                        "pattern": "/tmp/*",
                        "action": "allow",
                    }
                ],
            )
            self.assertEqual(state["execution"]["external_reentry_count"], 1)
            self.assertIn("review_invalidated_at", state["execution"])

            client._pending_permissions = [
                {
                    "id": "per-external-turn-2",
                    "sessionID": client.session_id,
                    "action": "external_directory",
                    "resources": ["/tmp/*"],
                }
            ]
            reconciled = service._reconcile_pending_inputs(
                prepared["task_id"],
                client,
                client.session_id,
            )
            self.assertIsNone(reconciled.outcome)
            self.assertEqual(
                client.permission_replies[-1],
                (client.session_id, "per-external-turn-2", "once"),
            )

            def deny_exact_pattern(current):
                policy = deepcopy(current["permission_policy"])
                policy["rules"] = [
                    {
                        "permission": "external_directory",
                        "pattern": "/tmp/*",
                        "action": "deny",
                    }
                ]
                current["permission_policy"] = policy

            service.store.update(prepared["task_id"], deny_exact_pattern)
            client._pending_permissions = [
                {
                    "id": "per-external-turn-3",
                    "sessionID": client.session_id,
                    "action": "external_directory",
                    "resources": ["/tmp/*"],
                }
            ]
            service._reconcile_pending_inputs(
                prepared["task_id"],
                client,
                client.session_id,
            )
            self.assertEqual(
                client.permission_replies[-1],
                (client.session_id, "per-external-turn-3", "reject"),
            )

    def test_completed_task_can_reply_to_live_question_from_external_turn(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = FakeClient()
            service = self.make_service(tmp, client)
            prepared = self._prime_reviewing_task(service, source)
            client._pending_questions = [
                {
                    "id": "que-external-turn",
                    "sessionID": client.session_id,
                    "questions": [
                        {"header": "Choice", "question": "Continue the same task?"}
                    ],
                }
            ]

            result = service.reply(
                prepared["task_id"],
                "question",
                {
                    "request_id": "que-external-turn",
                    "answers": [["Continue"]],
                },
                timeout_seconds=2,
            )

            self.assertEqual(result["outcome"], "idle")
            self.assertEqual(result["phase"], Phase.COLLECTING)
            self.assertEqual(
                client.question_replies,
                [("que-external-turn", [["Continue"]])],
            )
            self.assertEqual(client.created_session_count, 1)
            self.assertEqual(client.prompt_count, 1)

    def test_resume_reopens_completed_task_only_when_live_session_changed(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = ExternalResumeClient()
            service = self.make_service(tmp, client)
            prepared = self._prime_reviewing_task(service, source)

            unchanged = service.wait(prepared["task_id"], timeout_seconds=2)
            self.assertEqual(unchanged["reason"], "current-state")
            self.assertEqual(unchanged["phase"], Phase.REVIEWING)

            client.external_busy = True
            projected = service.status(prepared["task_id"])
            self.assertEqual(projected["execution_state"], "RUNNING")
            resumed = service.wait(prepared["task_id"], timeout_seconds=2)
            state = service.store.load(prepared["task_id"])

            self.assertEqual(resumed["outcome"], "idle")
            self.assertEqual(resumed["phase"], Phase.COLLECTING)
            self.assertEqual(state["execution"]["external_reentry_count"], 1)
            self.assertEqual(client.created_session_count, 1)
            self.assertEqual(client.prompt_count, 1)

    def test_resume_reopens_aborted_task_after_new_live_session_activity(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = ExternalResumeClient()
            service = self.make_service(tmp, client)
            prepared = self._prime_reviewing_task(service, source)
            service.abort_task(prepared["task_id"])
            client.external_busy = True

            projected = service.status(prepared["task_id"])
            persisted = service.store.load(prepared["task_id"])
            self.assertEqual(projected["execution_state"], "RUNNING")
            self.assertEqual(persisted["execution_state"], "ABORTED")
            self.assertEqual(persisted["phase"], Phase.CANCELLED)

            resumed = service.wait(prepared["task_id"], timeout_seconds=2)
            state = service.store.load(prepared["task_id"])

            self.assertEqual(resumed["outcome"], "idle")
            self.assertEqual(resumed["phase"], Phase.COLLECTING)
            self.assertEqual(state["abort"]["state"], "SUPERSEDED")
            self.assertEqual(
                state["abort"]["superseded_by"],
                "external-session-activity",
            )
            self.assertEqual(state["execution"]["external_reentry_count"], 1)
            self.assertEqual(client.created_session_count, 1)
            self.assertEqual(client.prompt_count, 1)
            self.assertEqual(client.abort_count, 1)

    def test_resume_blocks_then_adopts_late_external_activity(self):
        for finishes in (False, True):
            with self.subTest(finishes=finishes), TemporaryDirectory() as tmp:
                source = create_repo(Path(tmp) / "source")
                client = LateExternalActivityClient(finishes=finishes)
                service = self.make_service(tmp, client)
                prepared = self._prime_reviewing_task(service, source)
                service.abort_task(prepared["task_id"])
                client.armed = True

                with service.wait_coordinator.attach(
                    prepared["task_id"], "req-late-activity"
                ) as lease:
                    resumed = service.resume_wait(prepared["task_id"], 2, lease)
                state = service.store.load(prepared["task_id"])

                self.assertEqual(state["execution"]["external_reentry_count"], 1)
                self.assertEqual(state["abort"]["state"], "SUPERSEDED")
                self.assertEqual(client.created_session_count, 1)
                self.assertEqual(client.prompt_count, 1)
                if finishes:
                    self.assertEqual(resumed["outcome"], "COMPLETED")
                    self.assertEqual(state["execution_state"], "COMPLETED")
                    self.assertEqual(state["phase"], Phase.COLLECTING)
                else:
                    self.assertEqual(resumed["outcome"], "INTERRUPTED")
                    self.assertEqual(state["execution_state"], "RUNNING")
                    self.assertEqual(state["phase"], Phase.PAUSED)

    def test_resume_block_without_activity_preserves_terminal_state(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = LateExternalActivityClient(finishes=True)
            client.activity_seen = True
            service = self.make_service(tmp, client)
            prepared = self._prime_reviewing_task(service, source)
            service.abort_task(prepared["task_id"])

            with service.wait_coordinator.attach(
                prepared["task_id"], "req-quiet-block"
            ) as lease:
                resumed = service.resume_wait(prepared["task_id"], 1, lease)
            state = service.store.load(prepared["task_id"])

            self.assertEqual(resumed["outcome"], "ABORTED")
            self.assertEqual(resumed["reason"], "current-state")
            self.assertEqual(state["execution_state"], "ABORTED")
            self.assertEqual(state["abort"]["state"], "COMPLETED")
            self.assertNotIn("external_reentry_count", state["execution"])

    def test_collect_reacquires_completed_external_turn_after_abort(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = FakeClient()
            service = self.make_service(tmp, client)
            prepared = self._prime_reviewing_task(service, source)
            service.abort_task(prepared["task_id"])
            completed_at = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
            client.message_payload = [
                {
                    "info": {
                        "role": "assistant",
                        "createdAt": completed_at,
                    },
                    "parts": [
                        {"type": "text", "text": "EXTERNAL_TURN_DONE"},
                    ],
                }
            ]

            projected = service.status(prepared["task_id"])
            result = service.collect_result(prepared["task_id"])
            state = service.store.load(prepared["task_id"])

            self.assertEqual(projected["execution_state"], "COMPLETED")
            self.assertEqual(projected["phase"], Phase.COLLECTING)
            self.assertEqual(result["assistant_result"], "EXTERNAL_TURN_DONE")
            self.assertEqual(state["execution_state"], "COMPLETED")
            self.assertEqual(state["phase"], Phase.REVIEWING)
            self.assertEqual(state["review_state"], "REVIEWING")
            self.assertEqual(state["abort"]["state"], "SUPERSEDED")
            self.assertEqual(state["execution"]["external_reentry_count"], 1)
            self.assertEqual(client.created_session_count, 1)

    def test_collect_rejects_live_external_turn_still_running(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = FakeClient()
            service = self.make_service(tmp, client)
            prepared = self._prime_reviewing_task(service, source)
            client.scenario = "idle_busy"

            with self.assertRaisesRegex(ValueError, "resume_wait"):
                service.collect_result(prepared["task_id"])

            state = service.store.load(prepared["task_id"])
            self.assertEqual(state["execution_state"], "COMPLETED")
            self.assertEqual(state["phase"], Phase.REVIEWING)

    def test_collect_reconciles_running_task_from_finished_transcript(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = FakeClient()
            service = self.make_service(tmp, client)
            prepared = self._prime_reviewing_task(service, source)
            # Reset to the wedge shape: the task believes it is still RUNNING
            # (its SSE never saw session.idle), but the transcript ends on a
            # completed assistant turn with a step-finish.
            client.scenario = "idle_busy"

            def mark_running(current):
                current["execution_state"] = "RUNNING"
                current["phase"] = Phase.PAUSED
                current["review_state"] = "PENDING"
                current["execution"] = dict(current.get("execution") or {})
                current["execution"]["last_outcome"] = "timeout"

            service.store.update(prepared["task_id"], mark_running)
            completed_at = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
            client.message_payload = [
                {
                    "info": {
                        "role": "user",
                        "createdAt": completed_at,
                    },
                    "parts": [{"type": "text", "text": "PROCEED"}],
                },
                {
                    "info": {
                        "role": "assistant",
                        "createdAt": completed_at,
                        "completedAt": completed_at,
                    },
                    "parts": [
                        {"type": "step-start"},
                        {"type": "text", "text": "EXTERNAL_TURN_DONE"},
                        {"type": "step-finish"},
                    ],
                },
            ]

            result = service.collect_result(prepared["task_id"])
            state = service.store.load(prepared["task_id"])

            self.assertEqual(result["assistant_result"], "EXTERNAL_TURN_DONE")
            self.assertEqual(state["execution_state"], "COMPLETED")
            self.assertEqual(state["phase"], Phase.REVIEWING)
            self.assertEqual(state["review_state"], "REVIEWING")
            self.assertEqual(state["execution"]["external_reentry_count"], 1)
            self.assertEqual(client.created_session_count, 1)

    def test_collect_reconciles_stalled_task_from_finished_transcript(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = FakeClient()
            service = self.make_service(tmp, client)
            prepared = self._prime_reviewing_task(service, source)
            client.scenario = "idle_busy"

            def mark_stalled(current):
                current["execution_state"] = "STALLED"
                current["phase"] = Phase.STALLED
                current["review_state"] = "PENDING"

            service.store.update(prepared["task_id"], mark_stalled)
            completed_at = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
            client.message_payload = [
                {
                    "info": {
                        "role": "assistant",
                        "createdAt": completed_at,
                        "completedAt": completed_at,
                    },
                    "parts": [
                        {"type": "step-start"},
                        {"type": "text", "text": "STALLED_TURN_ACTUALLY_DONE"},
                        {"type": "step-finish"},
                    ],
                },
            ]

            result = service.collect_result(prepared["task_id"])
            state = service.store.load(prepared["task_id"])

            self.assertEqual(result["assistant_result"], "STALLED_TURN_ACTUALLY_DONE")
            self.assertEqual(state["execution_state"], "COMPLETED")
            self.assertEqual(state["phase"], Phase.REVIEWING)
            self.assertEqual(state["execution"]["external_reentry_count"], 1)

    def test_collect_does_not_reconcile_incomplete_trailing_turn(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = FakeClient()
            service = self.make_service(tmp, client)
            prepared = self._prime_reviewing_task(service, source)
            client.scenario = "idle_busy"

            def mark_running(current):
                current["execution_state"] = "RUNNING"
                current["phase"] = Phase.PAUSED
                current["review_state"] = "PENDING"

            service.store.update(prepared["task_id"], mark_running)
            activity_at = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
            client.message_payload = [
                {
                    "info": {
                        "role": "user",
                        "createdAt": activity_at,
                    },
                    "parts": [{"type": "text", "text": "CONTINUE"}],
                },
                {
                    "info": {"role": "assistant", "createdAt": activity_at},
                    "parts": [
                        {"type": "step-start"},
                        {"type": "reasoning", "text": ""},
                    ],
                },
            ]

            with self.assertRaisesRegex(ValueError, "RUNNING"):
                service.collect_result(prepared["task_id"])

            state = service.store.load(prepared["task_id"])
            self.assertEqual(state["execution_state"], "RUNNING")

    def test_continue_allows_stalled_task_to_nudge_wedged_session(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = ContinueOrderingClient()
            service = self.make_service(tmp, client)
            prepared = self._prime_reviewing_task(service, source)

            def mark_stalled(current):
                current["execution_state"] = "STALLED"
                current["phase"] = Phase.STALLED

            service.store.update(prepared["task_id"], mark_stalled)

            result = service.reply(
                prepared["task_id"],
                "continue",
                {"text": "The previous turn wedged; continue the same scope."},
                timeout_seconds=2,
            )
            state = service.store.load(prepared["task_id"])

            self.assertEqual(result["outcome"], "idle")
            self.assertEqual(client.created_session_count, 1)
            self.assertEqual(client.prompt_count, 2)
            self.assertIn(
                f"[oc-task:{prepared['task_id']}] continuation 1",
                client.last_prompt["text"],
            )
            self.assertEqual(state["execution_state"], "COMPLETED")
            self.assertEqual(
                state["execution"]["continuation"]["dispatch_state"],
                "SENT",
            )

    def test_continue_rejects_busy_stalled_session(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = ContinueOrderingClient()
            service = self.make_service(tmp, client)
            prepared = self._prime_reviewing_task(service, source)
            client.scenario = "idle_busy"

            def mark_stalled(current):
                current["execution_state"] = "STALLED"
                current["phase"] = Phase.STALLED

            service.store.update(prepared["task_id"], mark_stalled)

            with self.assertRaisesRegex(
                ValueError,
                "still busy; use resume_wait",
            ):
                service.reply(
                    prepared["task_id"],
                    "continue",
                    {"text": "Continue the same scope."},
                    timeout_seconds=2,
                )

            state = service.store.load(prepared["task_id"])
            self.assertEqual(state["execution_state"], "STALLED")
            self.assertEqual(client.prompt_count, 1)

    def test_large_unapproved_task_stops_before_worktree_creation(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            request = deepcopy(LOW_REQUEST)
            request["risk"]["file_count"] = 6
            service = self.make_service(tmp, FakeClient())

            state = service.prepare(source, "large", request, "http://127.0.0.1:4096")

            self.assertEqual(state["phase"], Phase.AWAITING_APPROVAL)
            self.assertEqual(state["worktree"], {})

    def test_selected_model_and_default_max_effort_are_reused_for_review(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            request = deepcopy(LOW_REQUEST)
            request["model"] = {"providerID": "mcli", "modelID": "glm-5.3"}
            client = FakeClient()
            service = self.make_service(tmp, client)

            prepared = service.prepare(source, "model-demo", request, "http://127.0.0.1:4096")
            service.dispatch(prepared["task_id"], timeout_seconds=2)
            service.collect(prepared["task_id"])
            service.reply(
                prepared["task_id"],
                "review",
                {"text": "Read-only recheck"},
                timeout_seconds=2,
            )

            self.assertEqual(
                [(call["model"], call["variant"]) for call in client.prompt_calls],
                [
                    ({"providerID": "mcli", "modelID": "glm-5.3"}, "max"),
                    ({"providerID": "mcli", "modelID": "glm-5.3"}, "max"),
                ],
            )
            state = service.status(prepared["task_id"])
            self.assertEqual(state["opencode"]["requested_model"], request["model"])
            self.assertEqual(state["opencode"]["effort"], "max")

    def test_prepare_rejects_partial_model_before_creating_task(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            request = deepcopy(LOW_REQUEST)
            request["model"] = {"modelID": "glm-5.3"}
            service = self.make_service(tmp, FakeClient())

            with self.assertRaisesRegex(ValueError, "providerID.*modelID"):
                service.prepare(source, "bad-model", request, "http://127.0.0.1:4096")

            self.assertFalse((Path(tmp) / "state/tasks").exists())

    def test_approve_review_records_evidence_and_advances_to_awaiting_integration(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = FakeClient()
            service = self.make_service(tmp, client)
            prepared = service.prepare(source, "approved", deepcopy(LOW_REQUEST), "http://127.0.0.1:4096")
            service.dispatch(prepared["task_id"], timeout_seconds=2)
            service.collect(prepared["task_id"])
            evidence = {
                "tests_passed": True,
                "review_summary": "Codex inspected all changed files and reran the contract tests.",
            }

            try:
                state = service.approve_review(prepared["task_id"], evidence)
            except AttributeError as error:
                self.fail(f"service cannot approve a completed review: {error}")

            self.assertEqual(state["phase"], Phase.AWAITING_INTEGRATION)
            self.assertEqual(state["review"]["tests_passed"], True)
            self.assertEqual(state["review"]["review_summary"], evidence["review_summary"])
            self.assertIn("approved_at", state["review"])

    def test_approve_review_rejects_missing_test_attestation(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = FakeClient()
            service = self.make_service(tmp, client)
            prepared = service.prepare(source, "unverified", deepcopy(LOW_REQUEST), "http://127.0.0.1:4096")
            service.dispatch(prepared["task_id"], timeout_seconds=2)
            service.collect(prepared["task_id"])

            try:
                approve = service.approve_review
            except AttributeError as error:
                self.fail(f"service cannot validate review approval: {error}")

            with self.assertRaisesRegex(ValueError, "tests_passed must be true"):
                approve(
                    prepared["task_id"],
                    {"tests_passed": False, "review_summary": "Review is not complete."},
                )
            self.assertEqual(service.status(prepared["task_id"])["phase"], Phase.REVIEWING)

    def test_permission_and_disconnect_pause_without_duplicate_dispatch(self):
        for scenario, expected_phase in (
            ("permission", Phase.PERMISSION_WAIT),
            ("disconnect", Phase.PAUSED),
        ):
            with self.subTest(scenario=scenario), TemporaryDirectory() as tmp:
                source = create_repo(Path(tmp) / "source")
                client = FakeClient()
                client.scenario = scenario
                service = self.make_service(tmp, client)
                prepared = service.prepare(source, scenario, deepcopy(LOW_REQUEST), "http://127.0.0.1:4096")

                outcome = service.dispatch(prepared["task_id"], timeout_seconds=2)

                self.assertEqual(outcome["phase"], expected_phase)
                self.assertEqual(client.prompt_count, 1)
                with self.assertRaisesRegex(ValueError, "cannot dispatch"):
                    service.dispatch(prepared["task_id"], timeout_seconds=2)

    def test_reconcile_answers_safe_permission_then_surfaces_next_unsafe_request(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = FakeClient()
            client._pending_permissions = [
                {
                    "id": "per_1",
                    "sessionID": "ses_fake_new",
                    "action": "read",
                    "resources": ["README.md"],
                },
                {
                    "id": "per_2",
                    "sessionID": "ses_fake_new",
                    "action": "external_directory",
                    "resources": ["/old/tree/a.py"],
                },
            ]
            service = self.make_service(tmp, client)
            prepared = service.prepare_task(source, "queued", deepcopy(LOW_REQUEST))
            state, _ = service._ensure_session(
                prepared["task_id"], client, service._request(prepared["task_id"])
            )

            result = service._reconcile_pending_inputs(
                prepared["task_id"], client, state["opencode"]["session_id"]
            )

            self.assertIsNotNone(result.outcome)
            self.assertEqual(result.outcome.kind, "permission")
            self.assertEqual(result.outcome.event["properties"]["id"], "per_2")
            self.assertEqual(
                client.permission_replies,
                [("ses_fake_new", "per_1", "once")],
            )
            audit = service.status(prepared["task_id"])["permission_audit"]
            self.assertEqual(audit[0]["request_id"], "per_1")
            self.assertEqual(audit[0]["response"], "once")
            self.assertEqual(audit[0]["decision"], "allow")
            self.assertEqual(
                service.status(prepared["task_id"])["progress"]["pending_permissions"][0]["request_id"],
                "per_2",
            )

    def test_native_safe_permission_with_empty_api_queue_replies_once_and_completes(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = FakeClient()
            client.scenario = "native_external_permission"
            request = deepcopy(LOW_REQUEST)
            request["permission_policy"] = {
                "rules": [
                    {
                        "permission": "external_directory",
                        "pattern": "/external/reference/**",
                        "action": "allow",
                    }
                ]
            }
            service = self.make_service(tmp, client)
            prepared = service.prepare_task(source, "native-external", request)

            result = service.dispatch(prepared["task_id"], timeout_seconds=2)
            state = service.status(prepared["task_id"])

            self.assertEqual(result["outcome"], "idle")
            self.assertEqual(
                client.permission_replies,
                [("ses_fake_new", "per_native_external", "once")],
            )
            self.assertEqual(client.prompt_count, 1)
            self.assertEqual(client.created_session_count, 1)
            self.assertEqual(len(state["permission_audit"]), 1)
            self.assertEqual(state["permission_audit"][0]["request_id"], "per_native_external")
            self.assertNotIn("native-secret", json.dumps(state))

    def test_native_undeclared_external_permission_surfaces_without_reply(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = FakeClient()
            client.scenario = "native_external_permission"
            service = self.make_service(tmp, client)
            prepared = service.prepare_task(source, "native-external-ask", deepcopy(LOW_REQUEST))

            result = service.dispatch(prepared["task_id"], timeout_seconds=2)
            state = service.status(prepared["task_id"])

            self.assertEqual(result["outcome"], "permission")
            self.assertEqual(result["event"]["properties"]["id"], "per_native_external")
            self.assertEqual(client.permission_replies, [])
            self.assertEqual(client.prompt_count, 1)
            self.assertEqual(client.created_session_count, 1)
            self.assertEqual(state["permission_audit"], [])
            self.assertNotIn("native-secret", json.dumps(result))

            replied = service.reply(
                prepared["task_id"],
                "permission",
                {
                    "request_id": "per_native_external",
                    "response": "once",
                    "user_approved": True,
                    "approval_basis": (
                        "Approve external_directory /external/reference/* once."
                    ),
                },
                timeout_seconds=2,
            )
            self.assertEqual(replied["outcome"], "idle")
            self.assertEqual(
                client.permission_replies,
                [("ses_fake_new", "per_native_external", "once")],
            )

    def test_native_p2_is_not_discarded_when_api_reconciliation_contains_p1(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = NativeP2WithApiP1Client()
            client._pending_permissions = [
                {
                    "id": "per-api-p1",
                    "sessionID": "ses_fake_new",
                    "action": "read",
                    "resources": ["README.md"],
                }
            ]
            service = self.make_service(tmp, client)
            prepared = service.prepare_task(source, "native-p2-api-p1", deepcopy(LOW_REQUEST))

            result = service.dispatch(prepared["task_id"], timeout_seconds=2)

            self.assertEqual(result["outcome"], "permission")
            self.assertEqual(result["event"]["properties"]["id"], "per-native-p2")
            self.assertEqual(
                client.permission_replies,
                [("ses_fake_new", "per-api-p1", "once")],
            )
            self.assertNotIn("native-p2-secret", json.dumps(result))

    def test_v2_pending_discovery_404_fails_closed_and_sanitizes_diagnostic(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = V2DiscoveryErrorClient()
            service = self.make_service(tmp, client)
            prepared = service.prepare_task(source, "v2-discovery-error", deepcopy(LOW_REQUEST))
            state, _ = service._ensure_session(
                prepared["task_id"], client, service._request(prepared["task_id"])
            )

            result = service._reconcile_pending_inputs(
                prepared["task_id"], client, state["opencode"]["session_id"]
            )
            persisted = service.store.load(prepared["task_id"])
            status = service.status(prepared["task_id"])

            self.assertEqual(result.outcome.kind, "disconnected")
            self.assertEqual(result.outcome.event["reason"], "pending-input-probe-failed")
            self.assertEqual(persisted["progress"]["diagnostic_error"]["status"], 404)
            self.assertEqual(
                persisted["progress"]["diagnostic_error"]["path"],
                "/api/session/ses_fake_new/permission",
            )
            self.assertNotIn("discovery-secret", json.dumps(persisted))
            self.assertNotIn("discovery-secret", json.dumps(status))
            self.assertNotIn("not found", json.dumps(status).lower())

    def test_legacy_absent_pending_discovery_404_is_an_empty_queue(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = LegacyDiscoveryAbsentClient()
            service = self.make_service(tmp, client)
            prepared = service.prepare_task(source, "legacy-discovery-absent", deepcopy(LOW_REQUEST))
            state, _ = service._ensure_session(
                prepared["task_id"], client, service._request(prepared["task_id"])
            )

            result = service._reconcile_pending_inputs(
                prepared["task_id"], client, state["opencode"]["session_id"]
            )

            self.assertIsNone(result.outcome)
            self.assertEqual(result.answered, [])

    def test_malformed_reconciliation_inputs_do_not_copy_nested_secrets(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = FakeClient()
            client._pending_permissions = [
                {
                    "id": "per-malformed",
                    "sessionID": "ses_fake_new",
                    "action": "read",
                    "resources": [{"path": "README.md", "token": "permission-secret"}],
                    "metadata": {"token": "metadata-secret"},
                }
            ]
            client._pending_questions = [
                {
                    "id": "que-malformed",
                    "sessionID": "ses_fake_new",
                    "questions": [
                        {
                            "header": {"token": "question-secret"},
                            "question": "Proceed?",
                            "tool": {"authorization": "Bearer question-secret"},
                        }
                    ],
                }
            ]
            service = self.make_service(tmp, client)
            prepared = service.prepare_task(source, "malformed-input", deepcopy(LOW_REQUEST))
            state, _ = service._ensure_session(
                prepared["task_id"], client, service._request(prepared["task_id"])
            )

            result = service._reconcile_pending_inputs(
                prepared["task_id"], client, state["opencode"]["session_id"]
            )
            serialized = json.dumps(
                {
                    "outcome": result.outcome.event if result.outcome else None,
                    "pending_permissions": result.pending_permissions,
                    "pending_questions": result.pending_questions,
                },
                ensure_ascii=False,
            )

            self.assertEqual(result.outcome.kind, "permission")
            self.assertEqual(result.outcome.event["properties"], {
                "id": "per-malformed",
                "sessionID": "ses_fake_new",
                "action": "read",
            })
            self.assertEqual(result.pending_permissions[0]["patterns"], [])
            self.assertEqual(result.pending_questions[0]["questions"], [])
            self.assertNotIn("secret", serialized)

    def test_unidentifiable_scoped_pending_entry_returns_sanitized_failure(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = FakeClient()
            client.pending_permissions = lambda session_id: [
                {
                    "id": "per-wrong-owner",
                    "sessionID": "ses_other",
                    "action": "read",
                    "resources": ["README.md"],
                    "metadata": {"authorization": "Bearer malformed-secret"},
                }
            ]
            service = self.make_service(tmp, client)
            prepared = service.prepare_task(source, "malformed-scoped-list", deepcopy(LOW_REQUEST))
            state, _ = service._ensure_session(
                prepared["task_id"], client, service._request(prepared["task_id"])
            )

            result = service._reconcile_pending_inputs(
                prepared["task_id"], client, state["opencode"]["session_id"]
            )
            persisted = service.store.load(prepared["task_id"])

            self.assertEqual(result.outcome.kind, "disconnected")
            self.assertEqual(
                result.outcome.event,
                {"reason": "pending-input-probe-failed"},
            )
            self.assertEqual(
                persisted["progress"]["diagnostic_error"]["message"],
                "OpenCode request failed",
            )
            self.assertNotIn("malformed-secret", json.dumps(persisted))
            self.assertNotIn("ses_other", json.dumps(persisted))

    def test_permission_reconciliation_behavior_matrix_is_fail_closed_and_bounded(self):
        cases = (
            (
                "project-always",
                {"action": "read", "resources": ["README.md"]},
                {"persistence": "project", "approval_basis": "approved-task"},
                None,
                "always",
            ),
            (
                "explicit-deny",
                {"action": "read", "resources": ["README.md"]},
                {"rules": [{"permission": "read", "pattern": "README.md", "action": "deny"}]},
                None,
                "reject",
            ),
            (
                "unknown-ask",
                {"action": "unknown_capability", "resources": ["README.md"]},
                {},
                "permission",
                None,
            ),
        )
        for slug, pending, policy, expected_kind, expected_response in cases:
            with self.subTest(slug=slug), TemporaryDirectory() as tmp:
                source = create_repo(Path(tmp) / "source")
                client = FakeClient()
                client._pending_permissions = [
                    {"id": f"per-{slug}", "sessionID": "ses_fake_new", **pending}
                ]
                request = deepcopy(LOW_REQUEST)
                request["permission_policy"] = policy
                service = self.make_service(tmp, client)
                prepared = service.prepare_task(source, slug, request)
                state, _ = service._ensure_session(
                    prepared["task_id"], client, service._request(prepared["task_id"])
                )

                result = service._reconcile_pending_inputs(
                    prepared["task_id"], client, state["opencode"]["session_id"]
                )

                if expected_kind is None:
                    self.assertIsNone(result.outcome)
                    self.assertEqual(client.permission_replies[0][2], expected_response)
                else:
                    self.assertEqual(result.outcome.kind, expected_kind)
                    if expected_response is not None:
                        self.assertEqual(client.permission_replies[0][2], expected_response)
                    else:
                        self.assertEqual(client.permission_replies, [])

    def test_pending_question_is_surfaced_without_a_permission_reply(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = FakeClient()
            client._pending_questions = [
                {
                    "id": "que-pending",
                    "sessionID": "ses_fake_new",
                    "questions": [{"header": "Choice", "question": "Continue?"}],
                }
            ]
            service = self.make_service(tmp, client)
            prepared = service.prepare_task(source, "pending-question", deepcopy(LOW_REQUEST))
            state, _ = service._ensure_session(
                prepared["task_id"], client, service._request(prepared["task_id"])
            )

            result = service._reconcile_pending_inputs(
                prepared["task_id"], client, state["opencode"]["session_id"]
            )

            self.assertEqual(result.outcome.kind, "question")
            self.assertEqual(result.outcome.event["properties"]["id"], "que-pending")
            self.assertEqual(client.permission_replies, [])
            self.assertEqual(client.question_replies, [])

    def test_reconciliation_caps_automatic_permission_replies_at_one_hundred(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = FakeClient()
            client._pending_permissions = [
                {
                    "id": f"per-{index}",
                    "sessionID": "ses_fake_new",
                    "action": "read",
                    "resources": ["README.md"],
                }
                for index in range(101)
            ]
            service = self.make_service(tmp, client)
            prepared = service.prepare_task(source, "reply-cap", deepcopy(LOW_REQUEST))
            state, _ = service._ensure_session(
                prepared["task_id"], client, service._request(prepared["task_id"])
            )

            result = service._reconcile_pending_inputs(
                prepared["task_id"], client, state["opencode"]["session_id"]
            )

            self.assertEqual(len(client.permission_replies), 100)
            self.assertEqual(result.outcome.kind, "permission")
            self.assertEqual(result.outcome.event["properties"]["id"], "per-100")

    def test_duplicate_permission_request_ids_are_replied_once(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = FakeClient()
            client._pending_permissions = [
                {
                    "id": "per-duplicate",
                    "sessionID": "ses_fake_new",
                    "action": "read",
                    "resources": ["README.md"],
                },
                {
                    "id": "per-duplicate",
                    "sessionID": "ses_fake_new",
                    "action": "read",
                    "resources": ["README.md"],
                },
            ]
            service = self.make_service(tmp, client)
            prepared = service.prepare_task(source, "duplicate-request", deepcopy(LOW_REQUEST))
            state, _ = service._ensure_session(
                prepared["task_id"], client, service._request(prepared["task_id"])
            )

            result = service._reconcile_pending_inputs(
                prepared["task_id"], client, state["opencode"]["session_id"]
            )

            self.assertIsNone(result.outcome)
            self.assertEqual(client.permission_replies, [("ses_fake_new", "per-duplicate", "once")])

    def test_permission_reply_exception_fails_closed_without_recording_reply(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = ReplyErrorClient()
            client._pending_permissions = [
                {
                    "id": "per-reply-error",
                    "sessionID": "ses_fake_new",
                    "action": "read",
                    "resources": ["README.md"],
                }
            ]
            service = self.make_service(tmp, client)
            prepared = service.prepare_task(source, "reply-error", deepcopy(LOW_REQUEST))
            state, _ = service._ensure_session(
                prepared["task_id"], client, service._request(prepared["task_id"])
            )

            result = service._reconcile_pending_inputs(
                prepared["task_id"], client, state["opencode"]["session_id"]
            )
            persisted = service.store.load(prepared["task_id"])

            self.assertEqual(result.outcome.kind, "disconnected")
            self.assertEqual(persisted.get("permission_audit"), [])
            self.assertNotIn("reply-secret", json.dumps(persisted))

    def test_permission_reply_rejects_stale_id_and_requires_action_specific_evidence(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = FakeClient()
            risky = {
                "id": "per-risky",
                "sessionID": "ses_fake_new",
                "action": "external_directory",
                "resources": ["/undeclared/reference.txt"],
            }
            client._pending_permissions = [deepcopy(risky)]
            service = self.make_service(tmp, client)
            prepared = service.prepare_task(source, "permission-evidence", deepcopy(LOW_REQUEST))
            state, _ = service._ensure_session(
                prepared["task_id"], client, service._request(prepared["task_id"])
            )
            reconciliation = service._reconcile_pending_inputs(
                prepared["task_id"], client, state["opencode"]["session_id"]
            )
            service._record_outcome(
                prepared["task_id"],
                client,
                state["opencode"]["session_id"],
                reconciliation.outcome,
            )

            with self.assertRaisesRegex(ValueError, "current pending permission"):
                service.reply(
                    prepared["task_id"],
                    "permission",
                    {"request_id": "per-stale", "response": "once"},
                    timeout_seconds=2,
                )
            with self.assertRaisesRegex(ValueError, "user_approved"):
                service.reply(
                    prepared["task_id"],
                    "permission",
                    {"request_id": "per-risky", "response": "once"},
                    timeout_seconds=2,
                )
            with self.assertRaisesRegex(ValueError, "action-specific"):
                service.reply(
                    prepared["task_id"],
                    "permission",
                    {
                        "request_id": "per-risky",
                        "response": "once",
                        "user_approved": True,
                        "approval_basis": "approved",
                    },
                    timeout_seconds=2,
                )

            result = service.reply(
                prepared["task_id"],
                "permission",
                {
                    "request_id": "per-risky",
                    "response": "once",
                    "user_approved": True,
                    "approval_basis": (
                        "User approved external_directory once for "
                        "/undeclared/reference.txt"
                    ),
                },
                timeout_seconds=2,
            )
            persisted = service.store.load(prepared["task_id"])

            self.assertEqual(result["outcome"], "idle")
            self.assertEqual(client.permission_replies, [("ses_fake_new", "per-risky", "once")])
            self.assertEqual(persisted["permission_audit"][-1]["request_id"], "per-risky")
            self.assertTrue(persisted["permission_audit"][-1]["user_approved"])

    def test_safe_current_permission_reply_remains_compatible_without_evidence(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = FakeClient()
            safe = {
                "id": "per-safe-explicit",
                "sessionID": "ses_fake_new",
                "action": "read",
                "resources": ["README.md"],
            }
            client._pending_permissions = [deepcopy(safe)]
            service = self.make_service(tmp, client)
            prepared = service.prepare_task(source, "safe-permission-reply", deepcopy(LOW_REQUEST))
            state, _ = service._ensure_session(
                prepared["task_id"], client, service._request(prepared["task_id"])
            )
            normalized, visible_event, _ = service._safe_permission_projection(
                safe, state["opencode"]["session_id"]
            )

            def prime(current):
                current["execution_state"] = "INPUT_REQUIRED"
                current["phase"] = Phase.PERMISSION_WAIT
                current["execution"]["last_event"] = visible_event
                current["progress"]["pending_permissions"] = [
                    service._visible_permission(normalized)
                ]

            service.store.update(prepared["task_id"], prime)

            result = service.reply(
                prepared["task_id"],
                "permission",
                {"request_id": "per-safe-explicit", "response": "once"},
                timeout_seconds=2,
            )

            self.assertEqual(result["outcome"], "idle")
            self.assertEqual(
                client.permission_replies,
                [("ses_fake_new", "per-safe-explicit", "once")],
            )

    def test_contract_prohibited_reply_requires_action_specific_evidence(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = FakeClient()
            command = "echo forbidden-marker"
            pending = {
                "id": "per-contract-prohibited",
                "sessionID": "ses_fake_new",
                "action": "bash",
                "resources": [command],
            }
            client._pending_permissions = [deepcopy(pending)]
            request = deepcopy(LOW_REQUEST)
            request["forbidden_actions"] = ["forbidden-marker"]
            service = self.make_service(tmp, client)
            prepared = service.prepare_task(source, "contract-prohibited", request)
            state, _ = service._ensure_session(
                prepared["task_id"], client, service._request(prepared["task_id"])
            )
            normalized, visible_event, _ = service._safe_permission_projection(
                pending, state["opencode"]["session_id"]
            )

            def prime(current):
                current["execution_state"] = "INPUT_REQUIRED"
                current["phase"] = Phase.PERMISSION_WAIT
                current["execution"]["last_event"] = visible_event
                current["progress"]["pending_permissions"] = [
                    service._visible_permission(normalized)
                ]

            service.store.update(prepared["task_id"], prime)

            with self.assertRaisesRegex(ValueError, "user_approved"):
                service.reply(
                    prepared["task_id"],
                    "permission",
                    {"request_id": "per-contract-prohibited", "response": "once"},
                    timeout_seconds=2,
                )

    def test_fifty_heartbeats_trigger_one_probe_and_no_prompt_or_model_call(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = HeartbeatBurstClient()
            service = self.make_service(tmp, client)
            prepared = self._prime_busy_task(service, client, source, "heartbeat-burst")
            state = service.store.load(prepared["task_id"])

            with service.wait_coordinator.attach(prepared["task_id"], "heartbeat-burst") as lease:
                outcome = service._wait_for_events(
                    prepared["task_id"],
                    client,
                    state["opencode"]["session_id"],
                    2,
                    lease,
                    lambda: None,
                )

            self.assertEqual(outcome.kind, "disconnected")
            self.assertEqual(client.pending_permissions_calls, 1)
            self.assertEqual(client.pending_questions_calls, 1)
            self.assertEqual(client.prompt_count, 0)
            self.assertEqual(client.created_session_count, 1)

    def test_accepted_uncertain_prompt_is_resumable_without_resend_when_history_lacks_marker(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = AcceptedPromptUncertainClient()
            service = self.make_service(tmp, client)
            prepared = service.prepare_task(source, "accepted-uncertain", deepcopy(LOW_REQUEST))

            first = service.dispatch(prepared["task_id"], timeout_seconds=2)
            resumed = service.wait(prepared["task_id"], timeout_seconds=2)
            state = service.status(prepared["task_id"])

            self.assertEqual(first["outcome"], "disconnected")
            self.assertEqual(first["next_action"], "resume_wait")
            self.assertEqual(resumed["outcome"], "disconnected")
            self.assertEqual(resumed["next_action"], "resume_wait")
            self.assertEqual(client.prompt_attempts, 1)
            self.assertEqual(client.prompt_count, 1)
            self.assertEqual(client.created_session_count, 1)
            self.assertEqual(state["opencode"]["dispatch_state"], "UNCERTAIN")
            self.assertNotIn("prompt-secret", json.dumps({"first": first, "resumed": resumed}))

    def _prime_busy_task(self, service, client, source, slug="stalled"):
        prepared = service.prepare_task(source, slug, deepcopy(LOW_REQUEST))
        state, _ = service._ensure_session(
            prepared["task_id"], client, service._request(prepared["task_id"])
        )

        def prime(current):
            current["execution_state"] = "RUNNING"
            current["phase"] = Phase.RUNNING
            current["opencode"]["dispatch_state"] = "SENT"
            current["progress"]["last_progress_at"] = "2000-01-01T00:00:00+00:00"
            current["progress"]["last_progress_event"] = "message.part.updated"

        service.store.update(prepared["task_id"], prime)
        return prepared

    def test_resume_returns_stalled_for_busy_task_without_abort(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = FakeClient()
            client.scenario = "idle_busy"
            service = self.make_service(tmp, client)
            prepared = self._prime_busy_task(service, client, source)

            with service.wait_coordinator.attach(prepared["task_id"], "request-stalled") as lease:
                result = service.resume_wait(prepared["task_id"], 2, lease)

            self.assertEqual(result["outcome"], "STALLED")
            self.assertEqual(result["execution_state"], "STALLED")
            self.assertEqual(result["wait_state"], "DETACHED")
            self.assertEqual(result["phase"], "STALLED")
            self.assertEqual(result["next_action"], "inspect_stall")
            self.assertEqual(client.abort_count, 0)

    def test_pending_permission_wins_over_stall_preflight(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = FakeClient()
            client.scenario = "idle_busy"
            client._pending_permissions = [
                {
                    "id": "per_stall",
                    "sessionID": "ses_fake_new",
                    "action": "external_directory",
                    "resources": ["/old/tree/a.py"],
                }
            ]
            service = self.make_service(tmp, client)
            prepared = self._prime_busy_task(service, client, source, "stalled-input")

            with service.wait_coordinator.attach(prepared["task_id"], "request-stalled-input") as lease:
                result = service.resume_wait(prepared["task_id"], 2, lease)

            self.assertEqual(result["outcome"], "INPUT_REQUIRED")
            self.assertEqual(result["event"]["properties"]["id"], "per_stall")
            self.assertEqual(client.abort_count, 0)

    def test_recent_message_activity_skips_stall_preflight(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = FakeClient()
            client.scenario = "idle_busy"
            recent = datetime.now(timezone.utc).isoformat()
            client.message_payload = [
                {
                    "info": {
                        "role": "assistant",
                        "createdAt": recent,
                    },
                    "parts": [{"type": "tool", "tool": "read", "state": {"status": "running"}}],
                }
            ]
            service = self.make_service(tmp, client)
            prepared = self._prime_busy_task(service, client, source, "recent-progress")
            state = service.status(prepared["task_id"])
            session_id = state["opencode"]["session_id"]

            self.assertIsNone(service._preflight_wait(prepared["task_id"], client, session_id))

    def test_recent_tool_end_time_from_missed_sse_prevents_false_stall(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = FakeClient()
            client.scenario = "idle_busy"
            recent = datetime.now(timezone.utc).isoformat()
            client.message_payload = [
                {
                    "info": {
                        "role": "assistant",
                        "time": {"created": "2000-01-01T00:00:00+00:00"},
                    },
                    "parts": [
                        {
                            "id": "part-recent-end",
                            "type": "tool",
                            "callID": "call-recent-end",
                            "tool": "read",
                            "state": {
                                "status": "completed",
                                "time": {
                                    "start": "2000-01-01T00:00:01+00:00",
                                    "end": recent,
                                },
                                "output": "tool-output-secret",
                            },
                        }
                    ],
                }
            ]
            service = self.make_service(tmp, client)
            prepared = self._prime_busy_task(service, client, source, "recent-tool-end")
            state = service.status(prepared["task_id"])
            session_id = state["opencode"]["session_id"]

            self.assertIsNone(service._preflight_wait(prepared["task_id"], client, session_id))
            persisted = service.store.load(prepared["task_id"])
            self.assertEqual(
                persisted["progress"]["last_progress_at"],
                recent,
            )
            self.assertNotIn("tool-output-secret", json.dumps(persisted))

    def test_far_future_message_time_cannot_suppress_stall_preflight(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = FakeClient()
            client.scenario = "idle_busy"
            client.message_payload = [
                {
                    "info": {
                        "role": "assistant",
                        "createdAt": "2999-01-01T00:00:00+00:00",
                    },
                    "parts": [],
                }
            ]
            service = self.make_service(tmp, client)
            prepared = self._prime_busy_task(service, client, source, "future-progress")
            state = service.status(prepared["task_id"])
            session_id = state["opencode"]["session_id"]

            outcome = service._preflight_wait(
                prepared["task_id"], client, session_id
            )

            self.assertIsNotNone(outcome)
            self.assertEqual(outcome.kind, "stalled")
            persisted = service.store.load(prepared["task_id"])
            self.assertNotEqual(
                persisted["progress"]["last_progress_at"],
                "2999-01-01T00:00:00+00:00",
            )

    def test_non_busy_preflight_reconciles_completion(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = FakeClient()
            service = self.make_service(tmp, client)
            prepared = self._prime_busy_task(service, client, source, "complete-preflight")
            state = service.status(prepared["task_id"])
            session_id = state["opencode"]["session_id"]

            outcome = service._preflight_wait(prepared["task_id"], client, session_id)

            self.assertIsNotNone(outcome)
            self.assertEqual(outcome.kind, "idle")

    def test_progress_snapshot_is_live_sanitized_and_read_only_when_unpersisted(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = FakeClient()
            client.scenario = "idle_busy"
            client._pending_permissions = [
                {
                    "id": "per-live",
                    "sessionID": "ses_fake_new",
                    "action": "read",
                    "resources": ["README.md"],
                    "metadata": {"token": "secret"},
                    "source": {
                        "messageID": "message-live",
                        "callID": "call-live",
                    },
                }
            ]
            client._pending_questions = [
                {
                    "id": "que-live",
                    "sessionID": "ses_fake_new",
                    "questions": [{"header": "Choice", "question": "Continue?"}],
                    "tool": {"token": "secret"},
                }
            ]
            client.message_payload = [
                {
                    "info": {"role": "assistant", "createdAt": "2026-08-29T23:23:00+00:00"},
                    "parts": [
                        {
                            "id": "part-live",
                            "type": "tool",
                            "callID": "call-live",
                            "tool": "read",
                            "state": {
                                "status": "running",
                                "time": {"start": 1788045720839},
                                "input": {"token": "secret"},
                            },
                        }
                    ],
                }
            ]
            service = self.make_service(tmp, client)
            prepared = service.prepare_task(source, "live-snapshot", deepcopy(LOW_REQUEST))
            state, _ = service._ensure_session(
                prepared["task_id"], client, service._request(prepared["task_id"])
            )
            before = deepcopy(service.store.load(prepared["task_id"])["progress"])

            snapshot = service._progress_snapshot(
                state,
                client,
                state["opencode"]["session_id"],
                persist=False,
            )

            self.assertEqual(snapshot["pending_permissions"][0]["request_id"], "per-live")
            self.assertEqual(snapshot["pending_questions"][0]["request_id"], "que-live")
            self.assertEqual(snapshot["pending_tools"][0]["name"], "read")
            self.assertEqual(snapshot["pending_tools"][0]["part_id"], "part-live")
            self.assertEqual(snapshot["pending_tools"][0]["call_id"], "call-live")
            self.assertEqual(snapshot["pending_tools"][0]["status"], "waiting_permission")
            self.assertEqual(
                snapshot["pending_tools"][0]["permission_request_id"],
                "per-live",
            )
            self.assertEqual(snapshot["session_status"], "busy")
            self.assertNotIn("secret", str(snapshot))
            self.assertEqual(
                service.store.load(prepared["task_id"])["progress"],
                before,
            )

    def test_abort_preserves_task_and_marks_cancelled(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = FakeClient()
            client.scenario = "disconnect"
            service = self.make_service(tmp, client)
            prepared = service.prepare(source, "abort", deepcopy(LOW_REQUEST), "http://127.0.0.1:4096")
            service.dispatch(prepared["task_id"], timeout_seconds=2)

            state = service.abort_task(prepared["task_id"])

            self.assertEqual(state["phase"], Phase.CANCELLED)
            self.assertEqual(state["execution_state"], "ABORTED")
            self.assertEqual(state["outcome"], "ABORTED")
            self.assertTrue(client.aborted)
            self.assertEqual(client.abort_count, 1)
            self.assertTrue((Path(tmp) / "state/tasks" / prepared["task_id"] / "state.json").is_file())

    def test_abort_failure_preserves_public_kind_and_sanitizes_diagnostic(self):
        with TemporaryDirectory() as tmp:
            source = create_repo(Path(tmp) / "source")
            client = AbortErrorClient()
            service = self.make_service(tmp, client)
            prepared = service.prepare_task(source, "abort-failure", deepcopy(LOW_REQUEST))
            state, _ = service._ensure_session(
                prepared["task_id"], client, service._request(prepared["task_id"])
            )

            result = service.abort_task(prepared["task_id"])
            persisted = service.store.load(prepared["task_id"])
            serialized = json.dumps({"result": result, "persisted": persisted})

            self.assertEqual(
                result["error"],
                {
                    "kind": "abort",
                    "status": 503,
                    "path": "/session/ses_fake_new/abort",
                },
            )
            self.assertEqual(result["outcome"], "FAILED")
            self.assertEqual(result["reason"], "abort-failed")
            self.assertEqual(persisted["abort"]["state"], "FAILED")
            self.assertEqual(persisted["abort"]["message"], "OpenCode request failed")
            self.assertEqual(client.abort_count, 1)
            self.assertNotIn("abort-secret", serialized)
            self.assertNotIn("Authorization", serialized)


if __name__ == "__main__":
    unittest.main()
