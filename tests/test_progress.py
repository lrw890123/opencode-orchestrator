import unittest

from opencode_orchestrator.progress import (
    idle_seconds,
    is_meaningful_progress,
    latest_message_progress_at,
    pending_tools,
)


SESSION_ID = "ses_target"


def event(event_type, session_id=SESSION_ID, **properties):
    return {
        "type": event_type,
        "properties": {"sessionID": session_id, **properties},
    }


class ProgressTest(unittest.TestCase):
    def test_transport_events_are_not_meaningful_progress(self):
        self.assertFalse(is_meaningful_progress(event("server.connected"), SESSION_ID))
        self.assertFalse(is_meaningful_progress(event("server.heartbeat"), SESSION_ID))

    def test_target_progress_events_are_meaningful(self):
        cases = [
            event("message.part.updated"),
            event("permission.v2.asked"),
            event("question.v2.asked"),
            event("message.part.updated", part={"type": "tool", "state": {"status": "completed"}}),
            event("file.edited"),
            event("session.idle"),
            event("session.error"),
        ]
        for candidate in cases:
            with self.subTest(event_type=candidate["type"]):
                self.assertTrue(is_meaningful_progress(candidate, SESSION_ID))

    def test_events_for_another_session_are_not_progress(self):
        self.assertFalse(
            is_meaningful_progress(event("message.part.updated", "ses_other"), SESSION_ID)
        )

    def test_pending_tools_are_sanitized_sorted_and_deduplicated(self):
        messages = [
            {
                "info": {"role": "assistant"},
                "parts": [
                    {
                        "id": "prt_2",
                        "type": "tool",
                        "callID": "call_2",
                        "tool": "bash",
                        "state": {
                            "status": "running",
                            "time": {"start": 1788045721839},
                            "input": {"secret": "hidden"},
                            "output": "hidden-output",
                            "metadata": {"authorization": "hidden-token"},
                        },
                    },
                    {
                        "id": "prt_1",
                        "type": "tool",
                        "callID": "call_1",
                        "tool": "read",
                        "state": {"status": "running", "time": {"start": 1788045720839}},
                    },
                    {
                        "id": "prt_1",
                        "type": "tool",
                        "callID": "call_1",
                        "tool": "read",
                        "state": {"status": "running", "time": {"start": 1788045720839}},
                    },
                ],
            }
        ]

        self.assertEqual(
            pending_tools(messages),
            [
                {
                    "name": "read",
                    "part_id": "prt_1",
                    "call_id": "call_1",
                    "status": "running",
                    "started_at": "2026-08-29T23:22:00.839000+00:00",
                },
                {
                    "name": "bash",
                    "part_id": "prt_2",
                    "call_id": "call_2",
                    "status": "running",
                    "started_at": "2026-08-29T23:22:01.839000+00:00",
                },
            ],
        )

    def test_pending_tools_ignore_malformed_parts_and_completed_tools(self):
        messages = [
            {"parts": [None, {}, {"type": "text", "text": "secret"}]},
            {
                "parts": [
                    {"type": "tool", "tool": "missing-id", "state": {"status": "running"}},
                    {
                        "id": "done",
                        "type": "tool",
                        "callID": "done-call",
                        "tool": "read",
                        "state": {"status": "completed", "time": {"start": 1}},
                    },
                ]
            },
        ]
        self.assertEqual(pending_tools(messages), [])

    def test_pending_tools_ignore_extreme_integer_timestamps(self):
        messages = [
            {
                "parts": [
                    {
                        "id": "extreme",
                        "type": "tool",
                        "callID": "extreme-call",
                        "tool": "read",
                        "state": {"status": "running", "time": {"start": 10**1000}},
                    }
                ]
            }
        ]

        self.assertEqual(pending_tools(messages), [])

    def test_latest_message_progress_ignores_extreme_integer_timestamps(self):
        messages = [{"info": {"created_at": 10**1000}, "parts": []}]

        self.assertIsNone(latest_message_progress_at(messages))

    def test_latest_message_progress_at_uses_message_timestamp_without_text(self):
        messages = [
            {
                "info": {"role": "assistant", "time": {"created": 1788045720839}},
                "parts": [{"type": "reasoning", "text": "secret"}],
            },
            {
                "info": {"role": "assistant", "createdAt": "2026-08-29T23:23:00+00:00"},
                "parts": [{"type": "text", "text": "public but excluded"}],
            },
        ]
        self.assertEqual(
            latest_message_progress_at(messages),
            "2026-08-29T23:23:00+00:00",
        )

    def test_latest_message_progress_uses_updates_completion_and_tool_state_times(self):
        messages = [
            {
                "info": {
                    "role": "assistant",
                    "time": {
                        "created": "2026-08-29T01:00:00+00:00",
                        "updated": "2026-08-29T02:00:00+00:00",
                        "completed": "2026-08-29T03:00:00+00:00",
                    },
                },
                "parts": [
                    {
                        "id": "tool-progress",
                        "type": "tool",
                        "callID": "call-progress",
                        "tool": "read",
                        "state": {
                            "status": "completed",
                            "time": {
                                "start": "2026-08-29T04:00:00+00:00",
                                "end": "2026-08-29T05:00:00+00:00",
                            },
                            "output": "progress-secret",
                        },
                    }
                ],
            },
            {
                "info": {
                    "createdAt": "2026-08-29T01:30:00+00:00",
                    "updatedAt": "2026-08-29T04:30:00+00:00",
                },
                "parts": [],
            },
        ]

        latest = latest_message_progress_at(messages)

        self.assertEqual(latest, "2026-08-29T05:00:00+00:00")
        self.assertNotIn("progress-secret", latest)

    def test_latest_message_progress_rejects_far_future_message_and_tool_times(self):
        messages = [
            {
                "info": {
                    "createdAt": "2026-08-29T23:23:00+00:00",
                    "updatedAt": "2999-01-01T00:00:00+00:00",
                },
                "parts": [
                    {
                        "type": "tool",
                        "state": {
                            "time": {"end": "2999-01-02T00:00:00+00:00"}
                        },
                    }
                ],
            }
        ]

        self.assertEqual(
            latest_message_progress_at(
                messages, observed_at="2026-08-30T00:00:00+00:00"
            ),
            "2026-08-29T23:23:00+00:00",
        )
        self.assertIsNone(
            latest_message_progress_at(
                [{"info": {"createdAt": "2999-01-01T00:00:00+00:00"}}],
                observed_at="2026-08-30T00:00:00+00:00",
            )
        )

    def test_idle_seconds_is_nonnegative_whole_seconds(self):
        self.assertEqual(
            idle_seconds(
                "2026-08-29T09:22:00+00:00",
                "2026-08-29T09:32:05+00:00",
            ),
            605,
        )
        self.assertEqual(
            idle_seconds(
                "2026-08-29T09:32:00+00:00",
                "2026-08-29T09:22:00+00:00",
            ),
            0,
        )


if __name__ == "__main__":
    unittest.main()
