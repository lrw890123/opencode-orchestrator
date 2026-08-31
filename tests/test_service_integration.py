from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from opencode_orchestrator.task_state import Phase
from tests.support.bridge_service import BridgeServiceHarness
from tests.support.fake_opencode import FakeOpenCodeServer
from tests.test_git_workspace import create_repo
from tests.test_service_integration_unit import LOW_REQUEST


class RealHttpServiceIntegrationTest(unittest.TestCase):
    def make_service(self, root: Path, server: FakeOpenCodeServer) -> BridgeServiceHarness:
        return BridgeServiceHarness(root / "state")

    def prepare(
        self, root: Path, server: FakeOpenCodeServer
    ) -> tuple[BridgeServiceHarness, dict]:
        source = create_repo(root / "source")
        service = self.make_service(root, server)
        state = service.prepare(source, "http-demo", deepcopy(LOW_REQUEST), server.base_url)
        return service, state

    def test_happy_path_uses_sse_and_review_reuses_session(self):
        with TemporaryDirectory() as tmp, FakeOpenCodeServer("idle") as server:
            root = Path(tmp)
            service, prepared = self.prepare(root, server)

            dispatched = service.dispatch(prepared["task_id"], timeout_seconds=5)
            result = service.collect(prepared["task_id"])
            before_review_session = result["session_id"]
            review = service.reply(
                prepared["task_id"],
                "review",
                {"text": "Read-only recheck"},
                timeout_seconds=5,
            )

            self.assertEqual(dispatched["outcome"], "idle")
            self.assertEqual(result["assistant_result"], "FAKE_DONE")
            self.assertFalse(result["poll_fallback_used"])
            self.assertEqual(review["outcome"], "idle")
            self.assertEqual(review["session_id"], before_review_session)
            self.assertEqual(server.prompt_count, 2)
            self.assertEqual(server.created_session_count, 1)
            self.assertEqual(server.session_directories, [prepared["worktree"]["path"]])

    def test_permission_question_and_error_events_map_to_phases(self):
        cases = (
            ("permission", "permission", Phase.PERMISSION_WAIT),
            ("question", "question", Phase.NEEDS_INPUT),
            ("error", "error", Phase.FAILED),
        )
        for scenario, expected_outcome, expected_phase in cases:
            with self.subTest(scenario=scenario), TemporaryDirectory() as tmp, FakeOpenCodeServer(scenario) as server:
                service, prepared = self.prepare(Path(tmp), server)

                result = service.dispatch(prepared["task_id"], timeout_seconds=5)

                self.assertEqual(result["outcome"], expected_outcome)
                self.assertEqual(result["phase"], expected_phase)
                self.assertNotIn("hidden", str(result))

    def test_queued_permissions_complete_from_api_reconciliation(self):
        with TemporaryDirectory() as tmp, FakeOpenCodeServer("queued_permissions") as server:
            service, prepared = self.prepare(Path(tmp), server)

            result = service.dispatch(prepared["task_id"], timeout_seconds=10)

            self.assertEqual(result["outcome"], "idle")
            self.assertEqual(server.created_session_count, 1)
            self.assertEqual(server.prompt_count, 1)
            session = next(iter(server.sessions.values()))
            self.assertEqual(
                [item["request_id"] for item in session.permission_replies],
                ["per_1", "per_2", "per_3"],
            )

    def test_native_safe_permission_completes_when_pending_apis_are_empty(self):
        with TemporaryDirectory() as tmp, FakeOpenCodeServer(
            "native_external_permission"
        ) as server:
            root = Path(tmp)
            source = create_repo(root / "source")
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
            service = self.make_service(root, server)
            prepared = service.prepare(source, "native-http", request, server.base_url)

            result = service.dispatch(prepared["task_id"], timeout_seconds=5)
            state = service.status(prepared["task_id"])
            session = next(iter(server.sessions.values()))

            self.assertEqual(result["outcome"], "idle")
            self.assertEqual(server.created_session_count, 1)
            self.assertEqual(server.prompt_count, 1)
            self.assertEqual(session.pending_permissions, [])
            self.assertEqual(
                [item["request_id"] for item in session.permission_replies],
                ["per_native_external"],
            )
            self.assertEqual(len(state["permission_audit"]), 1)
            self.assertNotIn("native-secret", str(state))

    def test_native_undeclared_external_permission_requires_input_with_empty_apis(self):
        with TemporaryDirectory() as tmp, FakeOpenCodeServer(
            "native_external_permission"
        ) as server:
            service, prepared = self.prepare(Path(tmp), server)

            result = service.dispatch(prepared["task_id"], timeout_seconds=5)
            session = next(iter(server.sessions.values()))

            self.assertEqual(result["outcome"], "permission")
            self.assertEqual(result["event"]["properties"]["id"], "per_native_external")
            self.assertEqual(server.created_session_count, 1)
            self.assertEqual(server.prompt_count, 1)
            self.assertEqual(session.pending_permissions, [])
            self.assertEqual(session.permission_replies, [])
            self.assertNotIn("native-secret", str(result))

    def test_sse_disconnect_resumes_without_duplicate_prompt(self):
        with TemporaryDirectory() as tmp, FakeOpenCodeServer("disconnect_then_idle") as server:
            service, prepared = self.prepare(Path(tmp), server)

            first = service.dispatch(prepared["task_id"], timeout_seconds=5)
            resumed = service.wait(prepared["task_id"], timeout_seconds=5)
            state = service.status(prepared["task_id"])

            self.assertEqual(first["outcome"], "disconnected")
            self.assertEqual(resumed["outcome"], "idle")
            self.assertEqual(server.prompt_count, 1)
            self.assertEqual(state["execution"]["sse_reconnects"], 1)
            self.assertFalse(state["execution"]["poll_fallback_used"])

    def test_uncertain_prompt_response_uses_marker_in_history_without_resend(self):
        with TemporaryDirectory() as tmp, FakeOpenCodeServer("uncertain_post") as server:
            service, prepared = self.prepare(Path(tmp), server)

            result = service.dispatch(prepared["task_id"], timeout_seconds=5)

            self.assertEqual(result["outcome"], "idle")
            self.assertEqual(server.prompt_count, 1)
            self.assertEqual(service.status(prepared["task_id"])["phase"], Phase.COLLECTING)

    def test_unsupported_effort_fails_before_session_creation_with_available_values(self):
        with TemporaryDirectory() as tmp, FakeOpenCodeServer("idle") as server:
            root = Path(tmp)
            source = create_repo(root / "source")
            request = deepcopy(LOW_REQUEST)
            request["model"] = {"providerID": "mcli", "modelID": "glm-5.3"}
            request["effort"] = "ultra"
            service = self.make_service(root, server)
            prepared = service.prepare(source, "bad-effort", request, server.base_url)

            result = service.dispatch(prepared["task_id"], timeout_seconds=5)

            self.assertEqual(result["outcome"], "configuration_error")
            self.assertEqual(result["phase"], Phase.FAILED)
            self.assertRegex(result["error"]["message"], r"fast, high, max")
            self.assertEqual(server.created_session_count, 0)


if __name__ == "__main__":
    unittest.main()
