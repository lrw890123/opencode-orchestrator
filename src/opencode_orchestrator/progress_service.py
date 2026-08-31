from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Callable

from .contracts import DEFAULT_STALL_TIMEOUT_SECONDS
from .event_stream import EventOutcome
from .opencode_client import OpenCodeError
from .progress import idle_seconds, latest_message_progress_at, pending_tools
from .task_state import ExecutionState, Phase, ReviewState, utc_now


class ProgressCoordinator:
    """Own live progress projection, stall detection, and terminal reentry."""

    def __init__(
        self,
        store,
        pending_inputs,
        diagnostic_projector: Callable[[Exception], dict],
    ) -> None:
        self.store = store
        self._pending_inputs = pending_inputs
        self._safe_diagnostic = diagnostic_projector

    @staticmethod
    def _timestamp_is_newer(candidate: object, current: object) -> bool:
        if not isinstance(candidate, str) or not candidate.strip():
            return False
        try:
            candidate_dt = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            if candidate_dt.tzinfo is None:
                candidate_dt = candidate_dt.replace(tzinfo=timezone.utc)
            candidate_dt = candidate_dt.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return False
        if not isinstance(current, str) or not current.strip():
            return True
        try:
            current_dt = datetime.fromisoformat(current.replace("Z", "+00:00"))
            if current_dt.tzinfo is None:
                current_dt = current_dt.replace(tzinfo=timezone.utc)
            current_dt = current_dt.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return True
        return candidate_dt > current_dt

    def _progress_snapshot(
        self,
        state: dict,
        client,
        session_id: str,
        *,
        persist: bool,
    ) -> dict:
        """Fetch one bounded, metadata-free live progress snapshot."""

        progress = deepcopy(state.get("progress") or {})
        if not isinstance(progress, dict):
            progress = {}
        progress.setdefault("last_progress_at", state.get("updated_at") or utc_now())
        progress.setdefault("last_progress_event", "task.unknown")
        progress.setdefault("idle_seconds", 0)
        progress.setdefault("heartbeat_count", 0)
        progress.setdefault("pending_tools", [])
        progress.setdefault("pending_permissions", [])
        progress.setdefault("pending_questions", [])
        progress.setdefault("diagnostic_error", None)
        progress.setdefault("last_input_probe_at", None)

        def pending(
            method_name: str,
            label: str,
            v2_template: str,
            legacy_path: str,
        ) -> list[dict]:
            method = getattr(client, method_name, None)
            if not callable(method):
                return []
            return self._pending_inputs._pending_fetch(
                client,
                session_id,
                method,
                label,
                v2_template,
                legacy_path,
            )

        try:
            raw_permissions = pending(
                "pending_permissions",
                "permission",
                "/api/session/{sessionID}/permission",
                "/permission",
            )
            raw_questions = pending(
                "pending_questions",
                "question",
                "/api/session/{sessionID}/question",
                "/question",
            )
            permissions: list[dict] = []
            for raw in raw_permissions:
                normalized, _event, _valid = self._pending_inputs._required_permission_projection(
                    raw, session_id
                )
                permissions.append(normalized)
            questions: list[dict] = []
            for raw in raw_questions:
                normalized, _event, _valid = self._pending_inputs._required_question_projection(
                    raw, session_id
                )
                questions.append(normalized)
            messages_method = getattr(client, "messages", None)
            messages = messages_method(session_id, limit=10000) if callable(messages_method) else []
            if not isinstance(messages, list):
                raise OpenCodeError("OpenCode returned an invalid message list")
            status_method = getattr(client, "session_status", None)
            session_status = status_method(session_id) if callable(status_method) else None
            if session_status is not None and not isinstance(session_status, dict):
                raise OpenCodeError("OpenCode returned an invalid session status")
            latest = latest_message_progress_at(messages)
            if self._timestamp_is_newer(latest, progress.get("last_progress_at")):
                progress["last_progress_at"] = latest
                progress["last_progress_event"] = "message.updated"
            progress["pending_permissions"] = [
                self._pending_inputs._visible_permission(item) for item in permissions
            ]
            progress["pending_tools"] = self._pending_inputs._mark_permission_waits(
                pending_tools(messages),
                progress["pending_permissions"],
            )
            progress["pending_questions"] = [
                self._pending_inputs._visible_question(item) for item in questions
            ]
            progress["session_status"] = (
                session_status.get("type")
                if isinstance(session_status, dict)
                and isinstance(session_status.get("type"), str)
                else None
            )
            try:
                progress["idle_seconds"] = idle_seconds(
                    progress["last_progress_at"], utc_now()
                )
            except (TypeError, ValueError):
                progress["idle_seconds"] = 0
            progress["diagnostic_error"] = None
        except Exception as error:
            progress["diagnostic_error"] = self._safe_diagnostic(error)
            if persist:
                def persist_diagnostic(current: dict) -> None:
                    current_progress = dict(current.get("progress") or {})
                    current_progress["diagnostic_error"] = deepcopy(
                        progress["diagnostic_error"]
                    )
                    current["progress"] = current_progress

                self.store.update(state["task_id"], persist_diagnostic)
            return progress

        if persist:
            def persist_progress(current: dict) -> None:
                current["progress"] = deepcopy(progress)

            self.store.update(state["task_id"], persist_progress)
        return progress

    def _terminal_live_projection(
        self,
        state: dict,
        progress: dict,
    ) -> tuple[str, str, str] | None:
        """Derive a live state when a terminal session changed out of band."""

        if state.get("execution_state") not in {
            ExecutionState.COMPLETED.value,
            ExecutionState.ABORTED.value,
        }:
            return None
        if progress.get("diagnostic_error") is not None:
            return None
        if progress.get("pending_permissions"):
            return (
                ExecutionState.INPUT_REQUIRED.value,
                Phase.PERMISSION_WAIT,
                ReviewState.REVISION_REQUESTED.value,
            )
        if progress.get("pending_questions"):
            return (
                ExecutionState.INPUT_REQUIRED.value,
                Phase.NEEDS_INPUT,
                ReviewState.REVISION_REQUESTED.value,
            )
        persisted_progress = state.get("progress") or {}
        terminal_progress_at = persisted_progress.get("last_progress_at")
        if state.get("execution_state") == ExecutionState.ABORTED.value:
            abort_completed_at = (state.get("abort") or {}).get("completed_at")
            if self._timestamp_is_newer(abort_completed_at, terminal_progress_at):
                terminal_progress_at = abort_completed_at
        newer_progress = self._timestamp_is_newer(
            progress.get("last_progress_at"),
            terminal_progress_at,
        )
        if progress.get("session_status") == "busy" or (
            progress.get("pending_tools") and newer_progress
        ):
            return (
                ExecutionState.RUNNING.value,
                Phase.RUNNING,
                ReviewState.REVISION_REQUESTED.value,
            )
        if newer_progress:
            return (
                ExecutionState.COMPLETED.value,
                Phase.COLLECTING,
                ReviewState.READY.value,
            )
        return None

    def _project_live_state(self, state: dict, progress: dict) -> dict:
        refreshed = deepcopy(state)
        refreshed_progress = deepcopy(progress)
        projection = self._terminal_live_projection(state, refreshed_progress)
        refreshed_progress["external_activity_detected"] = projection is not None
        refreshed["progress"] = refreshed_progress
        refreshed["requires_reacquire"] = projection is not None
        if projection is not None:
            (
                refreshed["execution_state"],
                refreshed["phase"],
                refreshed["review_state"],
            ) = projection
        return refreshed

    def _reopen_terminal_task(self, task_id: str, progress: dict | None = None) -> dict:
        """Persist a lease-owned transition for externally resumed session work."""

        detected_at = utc_now()

        def reopen(current: dict) -> None:
            if current.get("execution_state") not in {
                ExecutionState.COMPLETED.value,
                ExecutionState.ABORTED.value,
            }:
                return
            abort = current.get("abort") or {}
            if abort.get("state") in {"REQUESTED", "COMPLETED"}:
                superseded_abort = dict(abort)
                superseded_abort["state"] = "SUPERSEDED"
                superseded_abort["superseded_at"] = detected_at
                superseded_abort["superseded_by"] = "external-session-activity"
                current["abort"] = superseded_abort
            execution = dict(current.get("execution") or {})
            execution["external_reentry_count"] = int(
                execution.get("external_reentry_count", 0)
            ) + 1
            execution["external_reentry_at"] = detected_at
            if current.get("review_state") in {
                ReviewState.REVIEWING.value,
                ReviewState.PASSED.value,
                ReviewState.AWAITING_INTEGRATION.value,
            }:
                execution["review_invalidated_at"] = detected_at
            current["execution"] = execution
            current["execution_state"] = ExecutionState.RUNNING.value
            current["phase"] = Phase.RUNNING
            current["review_state"] = ReviewState.REVISION_REQUESTED.value
            if progress is not None:
                current_progress = deepcopy(progress)
                current_progress["external_activity_detected"] = True
                current["progress"] = current_progress

        return self.store.update(task_id, reopen)

    def _preflight_wait(self, task_id: str, client, session_id: str) -> EventOutcome | None:
        """Reconcile pending input and decide whether SSE waiting may proceed."""

        reconciliation = self._pending_inputs._reconcile_pending_inputs(
            task_id, client, session_id
        )
        if reconciliation.outcome is not None:
            return reconciliation.outcome
        current = self.store.load(task_id)
        progress = self._progress_snapshot(
            current,
            client,
            session_id,
            persist=True,
        )
        diagnostic = progress.get("diagnostic_error")
        if diagnostic is not None:
            return EventOutcome(
                "disconnected",
                {"reason": "progress-probe-failed", "properties": {"sessionID": session_id}},
                {},
            )
        pending_permissions = progress.get("pending_permissions") or []
        if pending_permissions:
            return EventOutcome(
                "permission",
                {
                    "type": "permission.reconciled",
                    "properties": {
                        "id": pending_permissions[0].get("request_id"),
                        "sessionID": pending_permissions[0].get("session_id"),
                        "action": pending_permissions[0].get("permission"),
                        "resources": deepcopy(pending_permissions[0].get("patterns", [])),
                    },
                },
                {},
            )
        pending_questions = progress.get("pending_questions") or []
        if pending_questions:
            return EventOutcome(
                "question",
                {
                    "type": "question.reconciled",
                    "properties": {
                        "id": pending_questions[0].get("request_id"),
                        "sessionID": pending_questions[0].get("session_id"),
                        "questions": deepcopy(pending_questions[0].get("questions", [])),
                    },
                },
                {},
            )
        if progress.get("session_status") != "busy":
            return EventOutcome(
                "idle",
                {"type": "session.idle", "properties": {"sessionID": session_id}},
                {},
            )
        policy = current.get("progress_policy") or {}
        threshold = policy.get(
            "stall_timeout_seconds", DEFAULT_STALL_TIMEOUT_SECONDS
        )
        if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 0:
            threshold = DEFAULT_STALL_TIMEOUT_SECONDS
        try:
            idle = idle_seconds(progress["last_progress_at"], utc_now())
        except (TypeError, ValueError):
            idle = threshold
        if idle < threshold:
            return None
        return EventOutcome(
            "stalled",
            {
                "reason": "idle-threshold",
                "properties": {"sessionID": session_id},
            },
            {},
        )
