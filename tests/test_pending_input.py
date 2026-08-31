import unittest

from opencode_orchestrator.pending_input import (
    PendingInputError,
    normalize_permission_request,
    normalize_question_request,
    permission_event,
    question_event,
)


class PendingInputNormalizationTest(unittest.TestCase):
    def test_normalizes_legacy_permission_fields_and_redacts_metadata(self):
        raw = {
            "id": "per_123",
            "sessionID": "ses_123",
            "permission": "external_directory",
            "patterns": ["/absolute/path/**"],
            "metadata": {"token": "must-not-escape"},
            "tool": {
                "messageID": "msg_123",
                "callID": "call_123",
                "secret": "must-not-escape",
            },
            "unexpected": "must-not-escape",
        }

        self.assertEqual(
            normalize_permission_request(raw, "ses_123"),
            {
                "request_id": "per_123",
                "session_id": "ses_123",
                "permission": "external_directory",
                "patterns": ["/absolute/path/**"],
                "metadata": {},
                "message_id": "msg_123",
                "call_id": "call_123",
            },
        )

    def test_normalizes_v2_permission_fields_and_source_ids(self):
        raw = {
            "id": "per_123",
            "sessionID": "ses_123",
            "action": "read",
            "resources": ["README.md"],
            "source": {"messageID": "msg_123", "callID": "call_123"},
            "metadata": {"authorization": "must-not-escape"},
        }

        normalized = normalize_permission_request(raw, "ses_123")
        self.assertEqual(normalized["permission"], "read")
        self.assertEqual(normalized["patterns"], ["README.md"])
        self.assertEqual(normalized["message_id"], "msg_123")
        self.assertEqual(normalized["call_id"], "call_123")
        self.assertEqual(normalized["metadata"], {})
        self.assertEqual(
            permission_event(normalized),
            {
                "type": "permission.reconciled",
                "properties": {
                    "id": "per_123",
                    "sessionID": "ses_123",
                    "action": "read",
                    "resources": ["README.md"],
                },
            },
        )

    def test_permission_source_and_tool_ids_are_optional(self):
        raw = {
            "id": "per_123",
            "sessionID": "ses_123",
            "permission": "read",
            "patterns": ["README.md"],
        }

        normalized = normalize_permission_request(raw, "ses_123")
        self.assertIsNone(normalized["message_id"])
        self.assertIsNone(normalized["call_id"])
        self.assertEqual(
            permission_event(normalized),
            {
                "type": "permission.reconciled",
                "properties": {
                    "id": "per_123",
                    "sessionID": "ses_123",
                    "action": "read",
                    "resources": ["README.md"],
                },
            },
        )

    def test_permission_rejects_missing_or_malformed_identity_targets_and_ownership(self):
        valid = {
            "id": "per_123",
            "sessionID": "ses_123",
            "action": "read",
            "resources": ["README.md"],
        }
        invalid = (
            {"id": "", "sessionID": "ses_123"},
            {"id": "per_123", "sessionID": "ses_123", "action": "read", "resources": []},
            {"id": "per_123", "sessionID": "ses_123", "action": "read", "resources": [" "]},
            {
                "id": "per_123",
                "sessionID": "ses_123",
                "action": "read",
                "resources": "README.md",
            },
            {
                "id": "per_123",
                "sessionID": "ses_other",
                "action": "read",
                "resources": ["README.md"],
            },
            {"id": "per_123", "sessionID": "ses_123", "action": "", "resources": ["README.md"]},
        )
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(PendingInputError):
                normalize_permission_request(raw, "ses_123")

        with self.assertRaises(PendingInputError):
            normalize_permission_request(valid, "ses_other")

    def test_normalizes_question_options_without_arbitrary_tool_metadata(self):
        raw = {
            "id": "que_123",
            "sessionID": "ses_123",
            "questions": [
                {
                    "header": "Choice",
                    "question": "Continue?",
                    "options": [
                        {
                            "label": "Yes",
                            "description": "Continue safely",
                            "value": "secret-internal-value",
                        },
                        {"label": "No", "description": "Stop"},
                    ],
                    "tool": {"token": "must-not-escape"},
                }
            ],
            "tool": {"messageID": "msg_123", "secret": "must-not-escape"},
        }

        normalized = normalize_question_request(raw, "ses_123")
        self.assertEqual(
            normalized,
            {
                "request_id": "que_123",
                "session_id": "ses_123",
                "questions": [
                    {
                        "header": "Choice",
                        "question": "Continue?",
                        "options": [
                            {"label": "Yes", "description": "Continue safely"},
                            {"label": "No", "description": "Stop"},
                        ],
                    }
                ],
            },
        )
        self.assertEqual(
            question_event(normalized),
            {
                "type": "question.reconciled",
                "properties": {
                    "id": "que_123",
                    "sessionID": "ses_123",
                    "questions": [
                        {
                            "header": "Choice",
                            "question": "Continue?",
                            "options": [
                                {"label": "Yes", "description": "Continue safely"},
                                {"label": "No", "description": "Stop"},
                            ],
                        }
                    ],
                },
            },
        )

    def test_question_rejects_wrong_session_and_malformed_user_visible_fields(self):
        valid = {
            "id": "que_123",
            "sessionID": "ses_123",
            "questions": [{"header": "Choice", "question": "Continue?", "options": []}],
        }
        with self.assertRaises(PendingInputError):
            normalize_question_request(valid, "ses_other")
        with self.assertRaises(PendingInputError):
            normalize_question_request({**valid, "questions": "Continue?"}, "ses_123")
        with self.assertRaises(PendingInputError):
            normalize_question_request(
                {
                    **valid,
                    "questions": [
                        {
                            "header": "Choice",
                            "question": "Continue?",
                            "options": [{"description": "missing label"}],
                        }
                    ],
                },
                "ses_123",
            )


if __name__ == "__main__":
    unittest.main()
