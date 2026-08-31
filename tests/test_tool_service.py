from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
import unittest

from opencode_orchestrator.cancellation import CancellationToken
from tests.test_service_integration_unit import LOW_REQUEST


TASK_ID = "oc-20260830-010101-a1b2c3d4"
COMMON_FIELDS = {
    "schema_version",
    "task_id",
    "outcome",
    "execution_state",
    "wait_state",
    "summary",
    "next_action",
    "artifacts",
}


class FakeCoordinator:
    def __init__(self):
        self.attached = []
        self.cancelled = []

    @contextmanager
    def attach(self, task_id, request_id):
        self.attached.append((task_id, request_id))
        yield SimpleNamespace(
            task_id=task_id,
            request_id=request_id,
            token=CancellationToken(),
        )

    def cancel_task(self, task_id, reason):
        self.cancelled.append((task_id, reason))
        return True


class FakeBridge:
    def __init__(self):
        self.wait_coordinator = FakeCoordinator()
        self.prepare_calls = []
        self.reply_calls = []
        self.abort_calls = []
        self.approvals = []
        self.reply_error = None
        self.state = {
            "schema_version": 3,
            "task_id": TASK_ID,
            "execution_state": "RUNNING",
            "wait_state": "DETACHED",
            "review_state": "PENDING",
            "phase": "PREPARING",
            "opencode": {"session_id": "ses_fake"},
        }

    def prepare_task(self, repo, slug, request, server_url):
        self.prepare_calls.append((repo, slug, deepcopy(request), server_url))
        return deepcopy(self.state)

    def dispatch_and_wait(self, task_id, timeout_seconds, lease):
        return {
            "schema_version": 3,
            "task_id": task_id,
            "outcome": "COMPLETED",
            "execution_state": "COMPLETED",
            "wait_state": "DETACHED",
            "summary": "done",
            "next_action": "collect_and_review",
            "artifacts": {},
        }

    def reply_and_wait(self, task_id, kind, payload, timeout_seconds, lease):
        if self.reply_error is not None:
            raise self.reply_error
        self.reply_calls.append((task_id, kind, deepcopy(payload), timeout_seconds))
        return self.dispatch_and_wait(task_id, timeout_seconds, lease)

    def resume_wait(self, task_id, timeout_seconds, lease):
        return self.dispatch_and_wait(task_id, timeout_seconds, lease)

    def status(self, task_id):
        return deepcopy(self.state)

    def read_transcript(self, task_id, cursor, limit, include_tool_output):
        return {"task_id": task_id, "messages": [], "next_cursor": None}

    def collect_result(self, task_id, review_evidence=None):
        self.state["execution_state"] = "COMPLETED"
        self.state["review_state"] = "REVIEWING"
        self.state["phase"] = "REVIEWING"
        return {"task_id": task_id, "changed_files": ["README.md"]}

    def approve_review(self, task_id, evidence):
        self.approvals.append((task_id, deepcopy(evidence)))
        self.state["review_state"] = "AWAITING_INTEGRATION"
        self.state["phase"] = "AWAITING_INTEGRATION"
        return deepcopy(self.state)

    def abort_task(self, task_id):
        self.abort_calls.append(task_id)
        self.state["execution_state"] = "ABORTED"
        self.state["wait_state"] = "CANCELLED"
        return {
            "schema_version": 3,
            "task_id": task_id,
            "outcome": "ABORTED",
            "execution_state": "ABORTED",
            "wait_state": "CANCELLED",
            "summary": "aborted",
            "next_action": "inspect_partial_result",
            "artifacts": {},
        }


class ToolServiceTest(unittest.TestCase):
    def definitions(self):
        from opencode_orchestrator.tools import TOOL_DEFINITIONS

        return TOOL_DEFINITIONS

    def make_service(self):
        from opencode_orchestrator.tool_service import ToolService

        bridge = FakeBridge()
        return ToolService(bridge), bridge

    def delegate_arguments(self):
        return {
            "repo_path": "/tmp/repo",
            "task_contract": deepcopy(LOW_REQUEST),
        }

    def test_tool_names_are_exact_and_unique(self):
        names = [tool["name"] for tool in self.definitions()]

        self.assertEqual(
            names,
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
        self.assertEqual(len(names), len(set(names)))

    def test_each_tool_has_common_output_schema_and_expected_annotations(self):
        definitions = {tool["name"]: tool for tool in self.definitions()}

        for name, tool in definitions.items():
            with self.subTest(name=name):
                self.assertEqual(set(tool["outputSchema"]["required"]), COMMON_FIELDS)
                self.assertIn("inputSchema", tool)
        self.assertTrue(definitions["task_status"]["annotations"]["readOnlyHint"])
        self.assertTrue(definitions["read_transcript"]["annotations"]["readOnlyHint"])
        self.assertTrue(definitions["abort_task"]["annotations"]["destructiveHint"])
        self.assertFalse(definitions["delegate_and_wait"]["annotations"]["readOnlyHint"])
        self.assertEqual(
            definitions["reply_and_wait"]["inputSchema"]["properties"]["kind"]["enum"],
            ["review", "continue", "permission", "question"],
        )
        permission_payload = definitions["reply_and_wait"]["inputSchema"]["properties"][
            "payload"
        ]["properties"]
        self.assertIn("remember_for_task", permission_payload)

    def test_delegate_schema_exposes_optional_policy_contracts(self):
        definition = next(tool for tool in self.definitions() if tool["name"] == "delegate_and_wait")
        properties = definition["inputSchema"]["properties"]
        self.assertIn("permission_policy", properties)
        self.assertIn("progress_policy", properties)
        self.assertNotIn("permission_policy", definition["inputSchema"]["required"])
        self.assertNotIn("progress_policy", definition["inputSchema"]["required"])

    def test_delegate_defaults_effort_and_timeout_then_attaches_request(self):
        tool_service, bridge = self.make_service()

        result = tool_service.call("delegate_and_wait", self.delegate_arguments(), "req-1")

        prepared_request = bridge.prepare_calls[0][2]
        self.assertEqual(prepared_request["effort"], "max")
        self.assertEqual(
            prepared_request["permission_policy"],
            {"default": "allow", "persistence": "task", "approval_basis": None, "rules": []},
        )
        self.assertEqual(
            prepared_request["progress_policy"],
            {"input_probe_interval_seconds": 15, "stall_timeout_seconds": 600},
        )
        self.assertEqual(bridge.wait_coordinator.attached, [(TASK_ID, "req-1")])
        self.assertTrue(COMMON_FIELDS.issubset(result))
        self.assertEqual(result["schema_version"], 3)

    def test_stalled_state_is_a_public_v3_outcome(self):
        tool_service, bridge = self.make_service()
        bridge.state["execution_state"] = "STALLED"
        bridge.state["phase"] = "STALLED"

        result = tool_service.call(
            "task_status", {"task_id": TASK_ID}, "req-stalled"
        )

        self.assertEqual(result["schema_version"], 3)
        self.assertEqual(result["outcome"], "STALLED")
        self.assertEqual(result["next_action"], "inspect_stall")

    def test_projected_external_completion_requires_reacquire_before_review(self):
        tool_service, bridge = self.make_service()
        bridge.state.update(
            {
                "execution_state": "COMPLETED",
                "phase": "COLLECTING",
                "review_state": "READY",
                "requires_reacquire": True,
            }
        )

        result = tool_service.call(
            "task_status", {"task_id": TASK_ID}, "req-reacquire"
        )

        self.assertEqual(result["outcome"], "COMPLETED")
        self.assertEqual(result["next_action"], "resume_wait")

    def test_delegate_normalizes_and_round_trips_explicit_policy_contracts(self):
        tool_service, bridge = self.make_service()
        permission_policy = {
            "default": "ask",
            "persistence": "task",
            "approval_basis": None,
            "rules": [
                {"permission": "read", "pattern": "src/**", "action": "allow"},
            ],
        }
        progress_policy = {
            "input_probe_interval_seconds": 30,
            "stall_timeout_seconds": 1200,
        }

        tool_service.call(
            "delegate_and_wait",
            {
                **self.delegate_arguments(),
                "permission_policy": permission_policy,
                "progress_policy": progress_policy,
            },
            "req-policy",
        )

        prepared_request = bridge.prepare_calls[0][2]
        self.assertEqual(prepared_request["permission_policy"], permission_policy)
        self.assertEqual(prepared_request["progress_policy"], progress_policy)
        self.assertIsNot(prepared_request["permission_policy"], permission_policy)
        self.assertIsNot(prepared_request["progress_policy"], progress_policy)

    def test_invalid_delegate_inputs_are_rejected_before_task_creation(self):
        from opencode_orchestrator.tool_service import ToolInputError

        cases = []
        relative = self.delegate_arguments()
        relative["repo_path"] = "relative/repo"
        cases.append(relative)
        bad_model = self.delegate_arguments()
        bad_model["model"] = {"modelID": "glm"}
        cases.append(bad_model)
        bad_timeout = self.delegate_arguments()
        bad_timeout["timeout_seconds"] = 86401
        cases.append(bad_timeout)
        extra = self.delegate_arguments()
        extra["unexpected"] = True
        cases.append(extra)
        relative_external = self.delegate_arguments()
        relative_external["permission_policy"] = {
            "rules": [
                {"permission": "external_directory", "pattern": "refs/**", "action": "allow"}
            ]
        }
        cases.append(relative_external)
        bad_probe = self.delegate_arguments()
        bad_probe["progress_policy"] = {"input_probe_interval_seconds": 4}
        cases.append(bad_probe)
        missing_basis = self.delegate_arguments()
        missing_basis["permission_policy"] = {"persistence": "project"}
        cases.append(missing_basis)

        for arguments in cases:
            with self.subTest(arguments=arguments):
                tool_service, bridge = self.make_service()
                with self.assertRaises(ToolInputError):
                    tool_service.call("delegate_and_wait", arguments, "req-invalid")
                self.assertEqual(bridge.prepare_calls, [])

    def test_unknown_tool_and_non_object_arguments_raise_tool_input_error(self):
        from opencode_orchestrator.tool_service import ToolInputError

        tool_service, _ = self.make_service()
        with self.assertRaisesRegex(ToolInputError, "unknown tool"):
            tool_service.call("made_up", {}, "req-1")
        with self.assertRaisesRegex(ToolInputError, "object"):
            tool_service.call("task_status", [], "req-2")

    def test_permission_reply_validates_optional_approval_evidence_shapes(self):
        from opencode_orchestrator.tool_service import ToolInputError

        tool_service, bridge = self.make_service()
        tool_service.call(
            "reply_and_wait",
            {
                "task_id": TASK_ID,
                "kind": "permission",
                "payload": {"request_id": "per-safe", "response": "once"},
            },
            "req-safe-permission",
        )
        self.assertEqual(bridge.reply_calls[0][2], {"request_id": "per-safe", "response": "once"})
        remembered = {
            "request_id": "per-external",
            "response": "once",
            "user_approved": True,
            "approval_basis": "Approve external_directory /tmp/* for this task.",
            "remember_for_task": True,
        }
        tool_service.call(
            "reply_and_wait",
            {"task_id": TASK_ID, "kind": "permission", "payload": remembered},
            "req-remembered-permission",
        )
        self.assertEqual(bridge.reply_calls[1][2], remembered)

        for payload in (
            {
                "request_id": "per-risky",
                "response": "once",
                "user_approved": "yes",
                "approval_basis": "approve bash rm",
            },
            {
                "request_id": "per-risky",
                "response": "once",
                "user_approved": True,
                "approval_basis": "   ",
            },
            {
                "request_id": "per-risky",
                "response": "once",
                "user_approved": True,
                "approval_basis": "Approve external_directory /tmp/*.",
                "remember_for_task": "yes",
            },
            {
                "request_id": "per-risky",
                "response": "always",
                "user_approved": True,
                "approval_basis": "Approve external_directory /tmp/*.",
                "remember_for_task": True,
            },
            {
                "request_id": "per-risky",
                "response": "once",
                "approval_basis": "Approve external_directory /tmp/*.",
                "remember_for_task": True,
            },
        ):
            with self.subTest(payload=payload), self.assertRaises(ToolInputError):
                tool_service.call(
                    "reply_and_wait",
                    {"task_id": TASK_ID, "kind": "permission", "payload": payload},
                    "req-invalid-permission",
                )

    def test_continue_reply_reuses_the_existing_reply_and_wait_tool(self):
        tool_service, bridge = self.make_service()

        tool_service.call(
            "reply_and_wait",
            {
                "task_id": TASK_ID,
                "kind": "continue",
                "payload": {"text": "Continue the same approved task."},
            },
            "req-continue",
        )

        self.assertEqual(
            bridge.reply_calls,
            [
                (
                    TASK_ID,
                    "continue",
                    {"text": "Continue the same approved task."},
                    3600,
                )
            ],
        )

    def test_deterministic_permission_reply_failure_is_invalid_input(self):
        from opencode_orchestrator.tool_service import ToolInputError

        tool_service, bridge = self.make_service()
        bridge.reply_error = ValueError("request_id is not a current pending permission")

        with self.assertRaisesRegex(ToolInputError, "no longer pending"):
            tool_service.call(
                "reply_and_wait",
                {
                    "task_id": TASK_ID,
                    "kind": "permission",
                    "payload": {"request_id": "per-stale", "response": "once"},
                },
                "req-stale-permission",
            )

    def test_sensitive_permission_reply_failure_gives_actionable_input_error(self):
        from opencode_orchestrator.tool_service import ToolInputError

        tool_service, bridge = self.make_service()
        bridge.reply_error = ValueError(
            "high-risk permission reply requires user_approved=true"
        )

        with self.assertRaisesRegex(
            ToolInputError,
            "user_approved=true.*action-specific approval_basis",
        ):
            tool_service.call(
                "reply_and_wait",
                {
                    "task_id": TASK_ID,
                    "kind": "permission",
                    "payload": {"request_id": "per-risky", "response": "once"},
                },
                "req-risky-permission",
            )

    def test_collect_with_review_evidence_uses_internal_approval(self):
        tool_service, bridge = self.make_service()
        evidence = {"tests_passed": True, "review_summary": "Reviewed and verified."}

        result = tool_service.call(
            "collect_result",
            {"task_id": TASK_ID, "review_evidence": evidence},
            "req-collect",
        )

        self.assertEqual(bridge.approvals, [(TASK_ID, evidence)])
        self.assertEqual(result["review_state"], "AWAITING_INTEGRATION")
        self.assertTrue(COMMON_FIELDS.issubset(result))

    def test_cancel_wait_never_routes_to_abort(self):
        tool_service, bridge = self.make_service()

        result = tool_service.call(
            "cancel_wait",
            {"task_id": TASK_ID, "reason": "user-stop"},
            "req-cancel",
        )

        self.assertEqual(bridge.wait_coordinator.cancelled, [(TASK_ID, "user-stop")])
        self.assertEqual(bridge.abort_calls, [])
        self.assertEqual(result["outcome"], "WAIT_CANCELLED")

    def test_abort_requires_explicit_task_id_and_routes_once(self):
        from opencode_orchestrator.tool_service import ToolInputError

        tool_service, bridge = self.make_service()
        with self.assertRaises(ToolInputError):
            tool_service.call("abort_task", {}, "req-abort-missing")

        result = tool_service.call("abort_task", {"task_id": TASK_ID}, "req-abort")

        self.assertEqual(bridge.abort_calls, [TASK_ID])
        self.assertEqual(result["outcome"], "ABORTED")


if __name__ == "__main__":
    unittest.main()
