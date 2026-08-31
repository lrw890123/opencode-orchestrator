import inspect
import json
import threading
import time
import unittest

from opencode_orchestrator import event_stream
from opencode_orchestrator.cancellation import CancellationToken
from opencode_orchestrator.event_stream import (
    EventOutcome,
    parse_sse_lines,
    relevant_event,
    wait_for_session,
)


def sse(payload: str) -> list[bytes]:
    return [f"data: {payload}\n".encode(), b"\n"]


class BlockingResponse:
    def __init__(self):
        self.started = threading.Event()
        self.closed = threading.Event()

    def __iter__(self):
        yield from sse('{"type":"server.connected","properties":{}}')
        self.started.set()
        self.closed.wait(timeout=2)

    def close(self):
        self.closed.set()


class EventStreamTest(unittest.TestCase):
    def test_wait_contract_accepts_cancellation_and_event_sink(self):
        parameters = inspect.signature(wait_for_session).parameters

        self.assertIn("cancellation", parameters)
        self.assertIn("on_event", parameters)
        self.assertIn("on_observed", parameters)

    def test_observer_receives_sanitized_heartbeat_and_cumulative_counts(self):
        lines = []
        lines += sse(
            json.dumps(
                {
                    "type": "server.heartbeat",
                    "properties": {
                        "sessionID": "ses_target",
                        "secret": "must-not-escape",
                        "metadata": {"authorization": "hidden-token"},
                    },
                }
            )
        )
        lines += sse(
            '{"type":"session.idle","properties":{"sessionID":"ses_target"}}'
        )
        observed = []

        outcome = wait_for_session(
            lines,
            "ses_target",
            on_connected=lambda: None,
            deadline=time.monotonic() + 1,
            on_observed=lambda event, counters: observed.append((event, counters.copy())),
        )

        self.assertEqual(outcome.kind, "idle")
        self.assertEqual(observed[0][0], {"type": "server.heartbeat", "properties": {"sessionID": "ses_target"}})
        self.assertEqual(observed[0][1], {"server.heartbeat": 1})
        self.assertNotIn("must-not-escape", json.dumps(observed))
        self.assertNotIn("hidden-token", json.dumps(observed))

    def test_observer_can_stop_stream_with_a_synthetic_outcome(self):
        lines = []
        lines += sse('{"type":"server.heartbeat","properties":{}}')
        lines += sse('{"type":"session.idle","properties":{"sessionID":"ses_target"}}')
        observed = []
        synthetic = EventOutcome(
            "stalled",
            {"reason": "idle-threshold"},
            {"server.heartbeat": 1},
        )

        def observe(event, counters):
            observed.append(event)
            if event["type"] == "server.heartbeat":
                return synthetic
            return None

        outcome = wait_for_session(
            lines,
            "ses_target",
            on_connected=lambda: None,
            deadline=time.monotonic() + 1,
            on_observed=observe,
        )

        self.assertEqual(outcome, synthetic)
        self.assertEqual([event["type"] for event in observed], ["server.heartbeat"])

    def test_cancellation_closes_a_blocking_response_and_returns_cancelled(self):
        response = BlockingResponse()
        cancellation = CancellationToken()
        outcomes = []
        worker = threading.Thread(
            target=lambda: outcomes.append(
                wait_for_session(
                    response,
                    "ses_target",
                    on_connected=lambda: None,
                    deadline=time.monotonic() + 5,
                    cancellation=cancellation,
                    on_event=lambda event: None,
                )
            )
        )
        worker.start()
        self.assertTrue(response.started.wait(timeout=1))

        cancellation.cancel("external")
        worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(outcomes[0].kind, "cancelled")
        self.assertEqual(outcomes[0].event, {"reason": "external"})

    def test_event_sink_receives_only_redacted_target_session_events(self):
        lines = []
        lines += sse('{"type":"server.connected","properties":{}}')
        lines += sse(json.dumps({
            "type": "message.part.updated",
            "properties": {
                "sessionID": "ses_other",
                "part": {"type": "reasoning", "text": "other secret"},
            },
        }))
        lines += sse(json.dumps({
            "type": "message.part.updated",
            "properties": {
                "sessionID": "ses_target",
                "part": {"type": "reasoning", "text": "target secret"},
            },
        }))
        lines += sse('{"type":"session.idle","properties":{"sessionID":"ses_target"}}')
        logged = []

        outcome = wait_for_session(
            lines,
            "ses_target",
            on_connected=lambda: None,
            deadline=time.monotonic() + 1,
            cancellation=CancellationToken(),
            on_event=logged.append,
        )

        self.assertEqual(outcome.kind, "idle")
        self.assertEqual(
            logged,
            [
                {"type": "message.part.updated", "properties": {"sessionID": "ses_target"}},
                {"type": "session.idle", "properties": {"sessionID": "ses_target"}},
            ],
        )
        self.assertNotIn("secret", json.dumps(logged))

    def test_progress_log_drops_reasoning_text_and_nested_metadata(self):
        raw = {
            "type": "message.part.updated",
            "properties": {
                "sessionID": "ses_target",
                "part": {"type": "reasoning", "text": "secret reasoning"},
                "metadata": {"authorization": "hidden-token"},
            },
        }

        self.assertTrue(
            hasattr(event_stream, "sanitize_event_for_log"),
            "sanitize_event_for_log is missing",
        )
        logged = event_stream.sanitize_event_for_log(raw)

        self.assertEqual(logged["type"], "message.part.updated")
        self.assertEqual(logged["properties"], {"sessionID": "ses_target"})
        self.assertNotIn("secret reasoning", str(logged))
        self.assertNotIn("hidden-token", str(logged))

    def test_progress_message_reports_safe_tool_state_without_inputs_or_output(self):
        raw = {
            "type": "message.part.updated",
            "properties": {
                "sessionID": "ses_target",
                "part": {
                    "type": "tool",
                    "tool": "bash",
                    "state": {
                        "status": "running",
                        "input": {"command": "echo Bearer secret-token"},
                        "output": "secret output",
                    },
                },
                "metadata": {"authorization": "Bearer hidden-token"},
            },
        }

        message = event_stream.progress_message(raw)

        self.assertEqual(message, "OpenCode is running bash")
        serialized = str(message)
        self.assertNotIn("secret-token", serialized)
        self.assertNotIn("hidden-token", serialized)
        self.assertNotIn("secret output", serialized)

    def test_progress_message_suppresses_heartbeats_and_reasoning_text(self):
        self.assertIsNone(
            event_stream.progress_message(
                {"type": "server.heartbeat", "properties": {"sessionID": "ses_target"}}
            )
        )
        message = event_stream.progress_message(
            {
                "type": "message.part.delta",
                "properties": {
                    "sessionID": "ses_target",
                    "delta": "private chain of thought",
                },
            }
        )
        self.assertEqual(message, "OpenCode is analyzing or editing")
        self.assertNotIn("private chain of thought", message)

    def test_progress_message_hides_unrecognized_tool_names(self):
        message = event_stream.progress_message(
            {
                "type": "message.part.updated",
                "properties": {
                    "sessionID": "ses_target",
                    "part": {
                        "type": "tool",
                        "tool": "Bearer_secret_provider_value",
                        "state": {"status": "running"},
                    },
                },
            }
        )

        self.assertEqual(message, "OpenCode is running a tool")
        self.assertNotIn("secret_provider_value", message)

    def test_progress_sink_ignores_other_sessions(self):
        lines = []
        lines += sse('{"type":"server.connected","properties":{}}')
        lines += sse(
            '{"type":"message.part.updated","properties":{"sessionID":"ses_other",'
            '"part":{"type":"tool","tool":"bash","state":{"status":"running"}}}}'
        )
        lines += sse(
            '{"type":"message.part.updated","properties":{"sessionID":"ses_target",'
            '"part":{"type":"tool","tool":"read","state":{"status":"running"}}}}'
        )
        lines += sse('{"type":"session.idle","properties":{"sessionID":"ses_target"}}')
        progress = []

        outcome = wait_for_session(
            lines,
            "ses_target",
            on_connected=lambda: None,
            deadline=time.monotonic() + 1,
            on_progress=progress.append,
        )

        self.assertEqual(outcome.kind, "idle")
        self.assertEqual(
            progress,
            [
                "OpenCode connection established",
                "OpenCode is running read",
                "OpenCode finished the current execution",
            ],
        )

    def test_error_log_keeps_bounded_diagnostic_fields_but_replaces_raw_message(self):
        raw = {
            "type": "session.error",
            "properties": {
                "sessionID": "ses_target",
                "error": {
                    "name": "ProviderError",
                    "code": "upstream_failed",
                    "status": 502,
                    "path": "/provider/request",
                    "message": "Authorization: Bearer secret-token",
                    "token": "secret-token",
                    "metadata": {"authorization": "Bearer secret-token"},
                },
            },
        }

        logged = event_stream.sanitize_event_for_log(raw)

        self.assertEqual(
            logged,
            {
                "type": "session.error",
                "properties": {
                    "sessionID": "ses_target",
                    "error": {
                        "name": "ProviderError",
                        "code": "upstream_failed",
                        "status": 502,
                        "path": "/provider/request",
                        "message": "OpenCode session failed",
                    },
                },
            },
        )
        self.assertNotIn("secret-token", json.dumps(logged))

    def test_parser_combines_multiline_data_and_blank_delimiter(self):
        lines = [
            b'data: {"type":"server.connected",\n',
            b'data: "properties":{}}\n',
            b"\n",
        ]

        events = list(parse_sse_lines(lines))

        self.assertEqual(events[0].payload["type"], "server.connected")

    def test_filter_keeps_only_target_terminal_or_input_events(self):
        target = {"type": "session.idle", "properties": {"sessionID": "ses_target"}}
        other = {"type": "session.idle", "properties": {"sessionID": "ses_other"}}
        noisy = {
            "type": "message.part.updated",
            "properties": {"sessionID": "ses_target", "part": {"type": "reasoning", "text": "secret"}},
        }

        self.assertTrue(relevant_event(target, "ses_target"))
        self.assertFalse(relevant_event(other, "ses_target"))
        self.assertFalse(relevant_event(noisy, "ses_target"))

    def test_wait_dispatches_after_connection_and_returns_idle(self):
        lines = []
        lines += sse('{"type":"server.connected","properties":{}}')
        lines += sse(
            '{"type":"message.part.updated","properties":{"sessionID":"ses_target",'
            '"part":{"type":"reasoning","text":"do not return"}}}'
        )
        lines += sse('{"type":"session.idle","properties":{"sessionID":"ses_target"}}')
        connected = []

        outcome = wait_for_session(
            lines,
            "ses_target",
            on_connected=lambda: connected.append(True),
            deadline=time.monotonic() + 1,
        )

        self.assertEqual(connected, [True])
        self.assertEqual(outcome.kind, "idle")
        self.assertEqual(outcome.event["type"], "session.idle")
        self.assertNotIn("do not return", str(outcome))
        self.assertEqual(outcome.counters["message.part.updated"], 1)

    def test_permission_event_is_redacted_to_decision_fields(self):
        lines = []
        lines += sse('{"type":"server.connected","properties":{}}')
        lines += sse(
            '{"type":"permission.v2.asked","properties":{'
            '"id":"per_1","sessionID":"ses_target","action":"bash",'
            '"resources":["git status"],"metadata":{"token":"hidden"}}}'
        )

        outcome = wait_for_session(
            lines,
            "ses_target",
            on_connected=lambda: None,
            deadline=time.monotonic() + 1,
        )

        self.assertEqual(outcome.kind, "permission")
        self.assertEqual(
            outcome.event["properties"],
            {
                "id": "per_1",
                "sessionID": "ses_target",
                "action": "bash",
                "resources": ["git status"],
            },
        )

    def test_input_event_sanitization_drops_nested_resources_and_metadata(self):
        permission = {
            "type": "permission.v2.asked",
            "properties": {
                "id": "per_nested",
                "sessionID": "ses_target",
                "action": "read",
                "resources": [{"path": "/safe.txt", "token": "permission-secret"}],
                "metadata": {"authorization": "Bearer permission-secret"},
            },
        }
        question = {
            "type": "question.v2.asked",
            "properties": {
                "id": "que_nested",
                "sessionID": "ses_target",
                "questions": [
                    {
                        "header": "Continue?",
                        "question": "Proceed with the task?",
                        "options": [
                            {
                                "label": "Yes",
                                "description": "Continue",
                                "secret": "question-secret",
                            },
                            {"label": {"token": "nested-question-secret"}},
                        ],
                        "metadata": {"token": "question-secret"},
                    },
                    {"header": {"token": "malformed-secret"}, "question": "Drop me"},
                ],
                "metadata": {"authorization": "Bearer question-secret"},
            },
        }

        permission_logged = event_stream.sanitize_event_for_log(permission)
        question_logged = event_stream.sanitize_event_for_log(question)

        self.assertEqual(
            permission_logged,
            {
                "type": "permission.v2.asked",
                "properties": {
                    "id": "per_nested",
                    "sessionID": "ses_target",
                    "action": "read",
                },
            },
        )
        self.assertEqual(
            question_logged,
            {
                "type": "question.v2.asked",
                "properties": {
                    "id": "que_nested",
                    "sessionID": "ses_target",
                    "questions": [
                        {
                            "header": "Continue?",
                            "question": "Proceed with the task?",
                            "options": [
                                {"label": "Yes", "description": "Continue"}
                            ],
                        }
                    ],
                },
            },
        )
        serialized = json.dumps([permission_logged, question_logged])
        self.assertNotIn("secret", serialized)
        self.assertNotIn("authorization", serialized.lower())


if __name__ == "__main__":
    unittest.main()
