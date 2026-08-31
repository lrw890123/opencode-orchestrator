from opencode_orchestrator.service import BridgeService


class BridgeServiceHarness(BridgeService):
    """Test-only synchronous facade for exercising wait operations directly."""

    @staticmethod
    def _legacy_wait_result(result: dict) -> dict:
        compatible = dict(result)
        raw = result.get("raw_outcome")
        if raw == "configuration_error":
            legacy_outcome = "configuration_error"
        elif raw in {
            "idle",
            "question",
            "permission",
            "error",
            "timeout",
            "disconnected",
            "cancelled",
            "stalled",
        }:
            legacy_outcome = raw
        else:
            legacy_outcome = {
                "COMPLETED": "idle",
                "INPUT_REQUIRED": "question",
                "FAILED": "error",
                "INTERRUPTED": "disconnected",
                "WAIT_CANCELLED": "cancelled",
                "ABORTED": "cancelled",
            }.get(result["outcome"], str(result["outcome"]).lower())
        compatible["outcome"] = legacy_outcome
        compatible["ok"] = legacy_outcome == "idle"
        return compatible

    def prepare(self, repo, slug, request, server_url="http://127.0.0.1:4096"):
        return self.prepare_task(repo, slug, request, server_url)

    def dispatch(self, task_id: str, timeout_seconds: int = 1800) -> dict:
        request_id = self._request_id("test-dispatch", task_id)
        with self.wait_coordinator.attach(task_id, request_id) as lease:
            return self._legacy_wait_result(
                self.dispatch_and_wait(task_id, timeout_seconds, lease)
            )

    def wait(self, task_id: str, timeout_seconds: int = 1800) -> dict:
        request_id = self._request_id("test-resume", task_id)
        with self.wait_coordinator.attach(task_id, request_id) as lease:
            return self._legacy_wait_result(
                self.resume_wait(task_id, timeout_seconds, lease)
            )

    def reply(
        self,
        task_id: str,
        kind: str,
        payload: dict,
        timeout_seconds: int = 1800,
    ) -> dict:
        request_id = self._request_id("test-reply", task_id)
        with self.wait_coordinator.attach(task_id, request_id) as lease:
            return self._legacy_wait_result(
                self.reply_and_wait(task_id, kind, payload, timeout_seconds, lease)
            )

    def collect(self, task_id: str) -> dict:
        return self.collect_result(task_id)

    def _safe_permission_projection(self, raw: dict, session_id: str):
        return self._pending_inputs._safe_permission_projection(raw, session_id)

    def _visible_permission(self, request: dict) -> dict:
        return self._pending_inputs._visible_permission(request)

    def _reconcile_pending_inputs(self, task_id: str, client, session_id: str):
        return self._pending_inputs._reconcile_pending_inputs(task_id, client, session_id)

    def _progress_snapshot(
        self, state: dict, client, session_id: str, *, persist: bool
    ) -> dict:
        return self._progress_service._progress_snapshot(
            state, client, session_id, persist=persist
        )

    def _preflight_wait(self, task_id: str, client, session_id: str):
        return self._progress_service._preflight_wait(task_id, client, session_id)

    def _task_fingerprint(self, base_sha: str, request: dict) -> str:
        return self._task_preparer.task_fingerprint(base_sha, request)
