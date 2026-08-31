from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import time
from typing import Callable

from .contracts import TASK_SCHEMA_VERSION
from .event_stream import EventOutcome, wait_for_session
from .opencode_client import OpenCodeClient, OpenCodeError, OpenCodeSelectionError
from .pending_input_service import PendingInputCoordinator
from .progress import is_meaningful_progress
from .progress_service import ProgressCoordinator
from .result_service import ResultService
from .task_preparation import TaskPreparer
from .task_state import (
    ExecutionState,
    Phase,
    ReviewState,
    TaskStore,
    WaitState,
    utc_now,
)
from .wait_coordinator import WaitCoordinator, WaitLease


OUTCOME_TO_STATE = {
    "idle": ("COMPLETED", "DETACHED", "COMPLETED"),
    "question": ("INPUT_REQUIRED", "DETACHED", "INPUT_REQUIRED"),
    "permission": ("INPUT_REQUIRED", "DETACHED", "INPUT_REQUIRED"),
    "error": ("FAILED", "DETACHED", "FAILED"),
    "timeout": ("RUNNING", "DETACHED", "INTERRUPTED"),
    "disconnected": ("RUNNING", "DETACHED", "INTERRUPTED"),
    "cancelled": ("RUNNING", "CANCELLED", "WAIT_CANCELLED"),
    "stalled": ("STALLED", "DETACHED", "STALLED"),
}

PHASE_BY_OUTCOME = {
    "idle": Phase.COLLECTING,
    "question": Phase.NEEDS_INPUT,
    "permission": Phase.PERMISSION_WAIT,
    "error": Phase.FAILED,
    "timeout": Phase.PAUSED,
    "disconnected": Phase.PAUSED,
    "cancelled": Phase.PAUSED,
    "stalled": Phase.STALLED,
}

SUMMARY_BY_OUTCOME = {
    "COMPLETED": "OpenCode completed the approved task.",
    "INPUT_REQUIRED": "OpenCode needs input before it can continue.",
    "FAILED": "OpenCode reported an execution failure.",
    "INTERRUPTED": "The local wait ended before completion was confirmed.",
    "WAIT_CANCELLED": "The local wait was cancelled; OpenCode was not aborted.",
    "ABORTED": "OpenCode execution was explicitly aborted.",
    "STALLED": "OpenCode made no meaningful progress before the stall threshold.",
}

NEXT_ACTION_BY_OUTCOME = {
    "COMPLETED": "collect_and_review",
    "INPUT_REQUIRED": "reply_and_wait",
    "FAILED": "inspect_failure",
    "INTERRUPTED": "resume_wait",
    "WAIT_CANCELLED": "resume_wait",
    "ABORTED": "inspect_partial_result",
    "STALLED": "inspect_stall",
}

SENSITIVE_PERMISSION_ASK_REASONS = frozenset(
    {
        "indeterminate-request",
        "indeterminate-target",
        "indeterminate-bash",
        "unknown-permission",
        "high-risk-action",
        "edit-outside-contract",
        "external-directory-not-declared",
        "unsupported-bash",
        "explicit-contract-prohibition",
    }
)


class BridgeService:
    def __init__(
        self,
        state_root: Path,
        client_factory: Callable = OpenCodeClient,
    ):
        self.state_root = Path(state_root).expanduser().resolve()
        self.store = TaskStore(self.state_root)
        self.client_factory = client_factory
        self.wait_coordinator = WaitCoordinator(self.store)
        self._task_preparer = TaskPreparer(self.state_root, self.store)
        self._pending_inputs = PendingInputCoordinator(
            self.store,
            self._request,
            self._safe_diagnostic,
            self._safe_identity,
        )
        self._progress_service = ProgressCoordinator(
            self.store,
            self._pending_inputs,
            self._safe_diagnostic,
        )
        self._result_service = ResultService(self.store, self._client)

    @staticmethod
    def _prompt_options(request: dict) -> dict:
        return {
            "model": deepcopy(request.get("model")),
            "variant": request["effort"],
        }

    def _client(self, state: dict):
        opencode = state["opencode"]
        return self.client_factory(
            opencode["base_url"],
            Path(opencode["directory"]),
            username=os.environ.get("OPENCODE_SERVER_USERNAME"),
            password=os.environ.get("OPENCODE_SERVER_PASSWORD"),
        )

    def _request(self, task_id: str) -> dict:
        with (self.store.task_dir(task_id) / "request.json").open(encoding="utf-8") as handle:
            return json.load(handle)

    @staticmethod
    def _safe_identity(value: object) -> bool:
        return isinstance(value, str) and bool(value) and value == value.strip()

    @staticmethod
    def _safe_diagnostic(error: Exception) -> dict:
        """Project client failures without persisting response bodies or metadata."""

        diagnostic = {"kind": "opencode"}
        status = getattr(error, "status", None)
        path = getattr(error, "path", None)
        if (
            isinstance(status, int)
            and not isinstance(status, bool)
            and 100 <= status <= 599
            and isinstance(path, str)
            and path.startswith("/")
            and len(path) <= 256
            and all(character.isalnum() or character in "/{}_.:-" for character in path)
        ):
            diagnostic.update({"status": status, "path": path})
            return diagnostic
        text = str(error)
        match = re.match(r"^OpenCode HTTP (?P<status>[0-9]{3}) for (?P<path>[^:]+):", text)
        if match is not None:
            try:
                status = int(match.group("status"))
            except (TypeError, ValueError):
                status = None
            path = match.group("path").strip()
            if (
                status is not None
                and path.startswith("/")
                and len(path) <= 256
                and all(character.isalnum() or character in "/{}_.:-" for character in path)
            ):
                diagnostic.update({"status": status, "path": path})
                return diagnostic
            diagnostic["message"] = "OpenCode HTTP request failed"
            return diagnostic
        if "connection" in text.lower():
            diagnostic["message"] = "OpenCode connection failed"
        else:
            diagnostic["message"] = "OpenCode request failed"
        return diagnostic


    def _prompt(self, state: dict, request: dict) -> str:
        marker = state["execution"]["dispatch_marker"]
        contract = json.dumps(request, ensure_ascii=False, indent=2, sort_keys=True)
        return (
            f"{marker}\n"
            "You are the execution agent. Follow this approved task contract exactly. "
            "Do not expand scope. Ask before acting if repository facts conflict with it.\n\n"
            f"Base commit: {state['source']['base_sha']}\n"
            f"Task fingerprint: {state['task_fingerprint']}\n"
            f"Task contract:\n{contract}\n\n"
            "Finish with a summary of changed files, tests run, unresolved issues, and risks."
        )

    @staticmethod
    def _messages_contain_marker(messages: list[dict], marker: str) -> bool:
        for message in messages:
            for part in message.get("parts") or []:
                if part.get("type") == "text" and marker in str(part.get("text", "")):
                    return True
        return False

    @staticmethod
    def _assistant_message_exists(messages: list[dict]) -> bool:
        return any(
            (message.get("info") or {}).get("role") == "assistant"
            and any(
                part.get("type") == "text" and part.get("text")
                for part in message.get("parts") or []
            )
            for message in messages
        )

    @staticmethod
    def _request_id(prefix: str, task_id: str) -> str:
        return f"{prefix}:{task_id}:{secrets.token_hex(8)}"

    def _observe_event(self, task_id: str, session_id: str):
        destination = self.store.task_dir(task_id) / "events.jsonl"

        def observe(event: dict, _counters: dict[str, int]) -> None:
            event_type = event.get("type") if isinstance(event, dict) else None
            if not isinstance(event_type, str) or not event_type.strip():
                return None
            properties = event.get("properties")
            target_session = (
                isinstance(properties, dict) and properties.get("sessionID") == session_id
            )
            recorded_at = utc_now()

            def persist(state: dict) -> None:
                execution = dict(state.get("execution", {}))
                event_counts = execution.get("event_counts")
                if not isinstance(event_counts, dict):
                    event_counts = {}
                else:
                    event_counts = dict(event_counts)
                previous = event_counts.get(event_type, 0)
                if not isinstance(previous, int) or isinstance(previous, bool) or previous < 0:
                    previous = 0
                event_counts[event_type] = previous + 1
                execution["event_counts"] = event_counts
                state["execution"] = execution

                progress = state.get("progress")
                if not isinstance(progress, dict):
                    progress = {}
                else:
                    progress = dict(progress)
                if event_type == "server.heartbeat":
                    heartbeat_count = progress.get("heartbeat_count", 0)
                    if (
                        not isinstance(heartbeat_count, int)
                        or isinstance(heartbeat_count, bool)
                        or heartbeat_count < 0
                    ):
                        heartbeat_count = 0
                    progress["heartbeat_count"] = heartbeat_count + 1
                if is_meaningful_progress(event, session_id):
                    progress["last_progress_at"] = recorded_at
                    progress["last_progress_event"] = event_type
                state["progress"] = progress

            self.store.update(task_id, persist)
            if target_session:
                destination.parent.mkdir(parents=True, exist_ok=True)
                record = {"recorded_at": recorded_at, "event": event}
                with destination.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
                    handle.write("\n")
            return None

        return observe

    def _result(
        self,
        state: dict,
        outcome: str,
        *,
        raw_outcome: str | None = None,
        reason: str | None = None,
        event: dict | None = None,
        counters: dict | None = None,
        artifacts: dict | None = None,
        error: dict | None = None,
    ) -> dict:
        result = {
            "schema_version": TASK_SCHEMA_VERSION,
            "ok": outcome in {"COMPLETED", "ABORTED"},
            "task_id": state["task_id"],
            "outcome": outcome,
            "execution_state": state["execution_state"],
            "wait_state": state["wait_state"],
            "review_state": state.get("review_state"),
            "opencode_session_id": state.get("opencode", {}).get("session_id"),
            "session_id": state.get("opencode", {}).get("session_id"),
            "phase": state.get("phase"),
            "summary": SUMMARY_BY_OUTCOME[outcome],
            "next_action": NEXT_ACTION_BY_OUTCOME[outcome],
            "artifacts": artifacts or {},
        }
        if raw_outcome is not None:
            result["raw_outcome"] = raw_outcome
        if reason is not None:
            result["reason"] = reason
        if event:
            result["event"] = event
        if counters:
            result["counters"] = counters
        if error is not None:
            result["error"] = error
        return result

    def _current_result(self, state: dict) -> dict:
        outcome = {
            ExecutionState.COMPLETED.value: "COMPLETED",
            ExecutionState.INPUT_REQUIRED.value: "INPUT_REQUIRED",
            ExecutionState.FAILED.value: "FAILED",
            ExecutionState.ABORTED.value: "ABORTED",
            ExecutionState.STALLED.value: "STALLED",
        }.get(state["execution_state"], "INTERRUPTED")
        return self._result(state, outcome, reason="current-state")

    def _record_outcome(
        self,
        task_id: str,
        client,
        session_id: str,
        outcome: EventOutcome,
        *,
        reason: str | None = None,
    ) -> dict:
        kind = outcome.kind
        if kind == "idle":
            try:
                status = client.session_status(session_id)
                messages = client.messages(session_id, limit=100)
                busy = isinstance(status, dict) and status.get("type") == "busy"
                reconciled = not busy and self._assistant_message_exists(messages)
            except OpenCodeError:
                reconciled = False
            if not reconciled:
                kind = "disconnected"
                reason = "idle-not-reconciled"

        execution_state, wait_state, public_outcome = OUTCOME_TO_STATE[kind]

        def persist(state: dict) -> None:
            abort = state.get("abort") or {}
            if (
                state.get("execution_state") == ExecutionState.ABORTED.value
                or abort.get("state") in {"REQUESTED", "COMPLETED"}
            ):
                return
            execution = dict(state.get("execution", {}))
            execution["last_event"] = outcome.event
            execution["last_outcome"] = kind
            state["execution"] = execution
            state["execution_state"] = execution_state
            state["wait_state"] = wait_state
            state["phase"] = PHASE_BY_OUTCOME[kind]
            if kind == "idle":
                state["review_state"] = ReviewState.READY.value

        state = self.store.update(task_id, persist)
        execution = state.get("execution") or {}
        persisted_counters = execution.get("event_counts")
        if not isinstance(persisted_counters, dict):
            persisted_counters = {}
        if state.get("execution_state") == ExecutionState.ABORTED.value:
            return self._result(state, "ABORTED", raw_outcome=kind)
        return self._result(
            state,
            public_outcome,
            raw_outcome=kind,
            reason=reason or (outcome.event.get("reason") if kind == "cancelled" else None),
            event=outcome.event,
            counters=dict(persisted_counters),
        )

    def _interrupted(
        self,
        task_id: str,
        client,
        session_id: str,
        reason: str,
        message: str | None = None,
    ) -> dict:
        event = {
            "type": "connection.error",
            "properties": {"sessionID": session_id},
        }
        if message:
            event["properties"]["message"] = self._safe_diagnostic(
                OpenCodeError(message)
            ).get("message", "OpenCode request failed")
        return self._record_outcome(
            task_id,
            client,
            session_id,
            EventOutcome("disconnected", event, {"connection.error": 1}),
            reason=reason,
        )

    def prepare_task(
        self,
        repo: Path,
        slug: str,
        request: dict,
        server_url: str = "http://127.0.0.1:4096",
    ) -> dict:
        return self._task_preparer.prepare_task(repo, slug, request, server_url)

    def _ensure_session(self, task_id: str, client, request: dict) -> tuple[dict, dict | None]:
        state = self.store.load(task_id)
        session_id = state.get("opencode", {}).get("session_id")
        dispatch_state = state.get("opencode", {}).get("dispatch_state")
        if (
            session_id
            and dispatch_state != "SENT"
            and state["execution_state"]
            in {ExecutionState.PREPARING.value, ExecutionState.RUNNING.value}
        ):
            return state, None
        if state["execution_state"] != ExecutionState.PREPARING.value:
            raise ValueError(f"cannot dispatch task in execution state {state['execution_state']}")
        if session_id:
            return state, None

        health = client.health()
        model = request.get("model")
        if model is not None:
            try:
                client.validate_model_selection(
                    model["providerID"],
                    model["modelID"],
                    request["effort"],
                )
            except OpenCodeSelectionError as error:
                failure = {"kind": "model_selection", "message": str(error)}

                def fail(current: dict) -> None:
                    current["execution_state"] = ExecutionState.FAILED.value
                    current["wait_state"] = WaitState.DETACHED.value
                    current["phase"] = Phase.FAILED
                    current["failure"] = failure

                failed = self.store.update(task_id, fail)
                return failed, self._result(
                    failed,
                    "FAILED",
                    raw_outcome="configuration_error",
                    reason="configuration-error",
                    error=failure,
                )

        session = client.create_session(
            f"{state['execution']['dispatch_marker']} {state['slug']}"
        )

        def record_session(current: dict) -> None:
            current["opencode"].update(
                {
                    "version": health.get("version"),
                    "session_id": session["id"],
                    "dispatch_state": "READY",
                }
            )
            current["phase"] = Phase.DISPATCHED

        return self.store.update(task_id, record_session), None

    def _dispatch_decision(self, task_id: str, client, session_id: str) -> tuple[bool, str | None]:
        state = self.store.load(task_id)
        dispatch_state = state["opencode"].get("dispatch_state", "READY")
        if dispatch_state == "SENT":
            return False, None
        if dispatch_state in {"SENDING", "UNCERTAIN"}:
            marker = state["execution"]["dispatch_marker"]
            try:
                messages = client.messages(session_id, limit=100)
            except OpenCodeError:
                return False, "dispatch-unresolved"
            if not isinstance(messages, list) or not messages:
                return False, "dispatch-unresolved"
            if self._messages_contain_marker(messages, marker):
                self.store.update(
                    task_id,
                    lambda current: current["opencode"].update({"dispatch_state": "SENT"}),
                )
                return False, None
            return False, "dispatch-unresolved"
        return True, None

    def _reconcile_continuation_dispatch(
        self, task_id: str, client, session_id: str
    ) -> str | None:
        """Recover an accepted continuation without ever resending it."""

        state = self.store.load(task_id)
        continuation = (state.get("execution") or {}).get("continuation")
        if not isinstance(continuation, dict):
            return None
        dispatch_state = continuation.get("dispatch_state")
        if dispatch_state == "SENT":
            return None
        if dispatch_state not in {"SENDING", "UNCERTAIN"}:
            return "continuation-invalid-state"
        marker = continuation.get("marker")
        if not self._safe_identity(marker):
            return "continuation-invalid-state"
        try:
            messages = client.messages(session_id, limit=100)
        except OpenCodeError:
            return "continuation-unresolved"
        if not isinstance(messages, list) or not self._messages_contain_marker(
            messages, marker
        ):
            return "continuation-unresolved"

        def recovered(current: dict) -> None:
            execution = dict(current.get("execution") or {})
            current_continuation = dict(execution.get("continuation") or {})
            if current_continuation.get("marker") != marker:
                return
            current_continuation["dispatch_state"] = "SENT"
            current_continuation["recovered_from"] = "message-history"
            current_continuation["recovered_at"] = utc_now()
            execution["continuation"] = current_continuation
            current["execution"] = execution

        self.store.update(task_id, recovered)
        return None

    def _send_initial_prompt(self, task_id: str, client, session_id: str, request: dict) -> None:
        def mark_sending(state: dict) -> None:
            state["execution_state"] = ExecutionState.RUNNING.value
            state["phase"] = Phase.RUNNING
            state["opencode"]["dispatch_state"] = "SENDING"

        state = self.store.update(task_id, mark_sending)
        client.prompt_async(
            session_id,
            self._prompt(state, request),
            **self._prompt_options(request),
        )
        self.store.update(
            task_id,
            lambda current: current["opencode"].update({"dispatch_state": "SENT"}),
        )

    def _wait_for_events(
        self,
        task_id: str,
        client,
        session_id: str,
        timeout_seconds: int,
        lease: WaitLease,
        on_connected: Callable[[], None],
        on_progress: Callable[[str], None] | None = None,
    ) -> EventOutcome:
        deadline = time.monotonic() + timeout_seconds
        callback_called = False
        deferred_connected_outcome: EventOutcome | None = None

        def connected_once() -> None:
            nonlocal callback_called
            if callback_called:
                return
            callback_called = True
            on_connected()

        def observe(event: dict, counters: dict[str, int]) -> EventOutcome | None:
            nonlocal deferred_connected_outcome
            event_type = event.get("type") if isinstance(event, dict) else None
            observed = self._observe_event(task_id, session_id)(event, counters)
            if observed is not None:
                return observed
            if event_type in {"question.asked", "question.v2.asked"}:
                return EventOutcome("question", event, counters)
            if event_type in {"permission.asked", "permission.v2.asked"}:
                # A native P2 can be queued behind an API-visible P1 on the
                # same newly connected stream.  Consume the concrete native
                # event before returning any API result captured at connect,
                # otherwise reconnect/input handling can discard P2 forever.
                return self._pending_inputs._handle_native_permission_event(
                    task_id,
                    client,
                    session_id,
                    event,
                    counters,
                )
            if deferred_connected_outcome is not None and event_type != "server.connected":
                outcome = deferred_connected_outcome
                deferred_connected_outcome = None
                return EventOutcome(outcome.kind, outcome.event, counters)
            if event_type == "server.heartbeat" and self._pending_inputs._input_probe_due(
                task_id
            ):
                before = self.store.load(task_id).get("permission_audit") or []
                before_count = len(before) if isinstance(before, list) else 0
                preflight = self._progress_service._preflight_wait(
                    task_id, client, session_id
                )
                if preflight is not None:
                    return preflight
                after = self.store.load(task_id).get("permission_audit") or []
                after_count = len(after) if isinstance(after, list) else 0
                if after_count > before_count:
                    return EventOutcome(
                        "reconnect",
                        {
                            "type": "pending-input.reconciled",
                            "properties": {"sessionID": session_id},
                        },
                        counters,
                    )
                return None
            should_probe = event_type == "server.connected"
            if should_probe:
                reconciliation = self._pending_inputs._reconcile_pending_inputs(
                    task_id, client, session_id
                )
                if reconciliation.outcome is not None:
                    deferred_connected_outcome = reconciliation.outcome
                    return None
                if reconciliation.answered:
                    deferred_connected_outcome = EventOutcome(
                        "reconnect",
                        {
                            "type": "pending-input.reconciled",
                            "properties": {"sessionID": session_id},
                        },
                        counters,
                    )
            return None

        while time.monotonic() <= deadline:
            with client.event_response() as response:
                outcome = wait_for_session(
                    response,
                    session_id,
                    on_connected=connected_once,
                    deadline=deadline,
                    cancellation=lease.token,
                    on_observed=observe,
                    on_progress=on_progress,
                )
            if deferred_connected_outcome is not None and outcome.kind in {
                "disconnected",
                "timeout",
            }:
                deferred = deferred_connected_outcome
                deferred_connected_outcome = None
                outcome = deferred
            if outcome.kind != "reconnect":
                return outcome
            self.store.update(
                task_id,
                lambda state: state["execution"].update(
                    {
                        "sse_reconnects": int(
                            state["execution"].get("sse_reconnects", 0)
                        )
                        + 1
                    }
                ),
            )
        return EventOutcome("timeout", {}, {})

    def dispatch_and_wait(
        self,
        task_id: str,
        timeout_seconds: int,
        lease: WaitLease,
        progress: Callable[[str], None] | None = None,
    ) -> dict:
        if lease.task_id != task_id:
            raise ValueError("wait lease task_id does not match dispatch task")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        request = self._request(task_id)
        state = self.store.load(task_id)
        client = self._client(state)
        try:
            state, failure = self._ensure_session(task_id, client, request)
        except OpenCodeError as error:
            return self._interrupted(task_id, client, "", "session-setup-failed", str(error))
        if failure is not None:
            return failure
        session_id = state["opencode"]["session_id"]
        send_prompt, decision_error = self._dispatch_decision(task_id, client, session_id)
        if decision_error:
            return self._interrupted(task_id, client, session_id, decision_error)
        if not send_prompt:
            reconciliation = self._pending_inputs._reconcile_pending_inputs(
                task_id, client, session_id
            )
            if reconciliation.outcome is not None:
                return self._record_outcome(
                    task_id,
                    client,
                    session_id,
                    reconciliation.outcome,
                    reason=(
                        reconciliation.outcome.event.get("reason")
                        if isinstance(reconciliation.outcome.event, dict)
                        else None
                    ),
                )

        prompt_sent = False
        def send() -> None:
            nonlocal prompt_sent
            if send_prompt and not prompt_sent:
                self._send_initial_prompt(task_id, client, session_id, request)
                prompt_sent = True

        try:
            outcome = self._wait_for_events(
                task_id,
                client,
                session_id,
                timeout_seconds,
                lease,
                send,
                progress,
            )
        except OpenCodeError as error:
            current = self.store.load(task_id)
            if current["opencode"].get("dispatch_state") == "SENDING":
                self.store.update(
                    task_id,
                    lambda state: state["opencode"].update({"dispatch_state": "UNCERTAIN"}),
                )
                _send_again, recovery_error = self._dispatch_decision(
                    task_id,
                    client,
                    session_id,
                )
                if recovery_error:
                    return self._interrupted(task_id, client, session_id, recovery_error, str(error))
                outcome = EventOutcome(
                    "idle",
                    {
                        "type": "session.idle",
                        "properties": {
                            "sessionID": session_id,
                            "recoveredFrom": "uncertain-post",
                        },
                    },
                    {"dispatch.uncertain_recovered": 1},
                )
                return self._record_outcome(task_id, client, session_id, outcome)
            return self._interrupted(
                task_id,
                client,
                session_id,
                "event-connection-failed",
                str(error),
            )
        return self._record_outcome(task_id, client, session_id, outcome)

    def resume_wait(
        self,
        task_id: str,
        timeout_seconds: int,
        lease: WaitLease,
        progress: Callable[[str], None] | None = None,
    ) -> dict:
        if lease.task_id != task_id:
            raise ValueError("wait lease task_id does not match resumed task")
        state = self.store.load(task_id)
        if state["execution_state"] == ExecutionState.FAILED.value:
            return self._current_result(state)
        if (
            state["execution_state"] == ExecutionState.INPUT_REQUIRED.value
            and not state.get("opencode", {}).get("session_id")
        ):
            return self._current_result(state)
        if state["execution_state"] in {
            ExecutionState.COMPLETED.value,
            ExecutionState.ABORTED.value,
        } and (
            state.get("opencode", {}).get("dispatch_state") != "SENT"
            or not state.get("opencode", {}).get("session_id")
        ):
            return self._current_result(state)
        if state.get("opencode", {}).get("dispatch_state") != "SENT":
            return self.dispatch_and_wait(task_id, timeout_seconds, lease)
        session_id = state["opencode"].get("session_id")
        if not session_id:
            raise ValueError("task has no OpenCode session to resume")
        client = self._client(state)

        if state["execution_state"] in {
            ExecutionState.COMPLETED.value,
            ExecutionState.ABORTED.value,
        }:
            live_progress = self._progress_service._progress_snapshot(
                state,
                client,
                session_id,
                persist=False,
            )
            if self._progress_service._terminal_live_projection(
                state, live_progress
            ) is None:
                return self._current_result(state)
            state = self._progress_service._reopen_terminal_task(
                task_id, live_progress
            )

        continuation_error = self._reconcile_continuation_dispatch(
            task_id, client, session_id
        )
        if continuation_error is not None:
            return self._interrupted(
                task_id,
                client,
                session_id,
                continuation_error,
            )

        preflight = self._progress_service._preflight_wait(
            task_id, client, session_id
        )
        if preflight is not None:
            return self._record_outcome(
                task_id,
                client,
                session_id,
                preflight,
                reason=(
                    preflight.event.get("reason")
                    if isinstance(preflight.event, dict)
                    else None
                ),
            )

        def mark_resumed(current: dict) -> None:
            execution = dict(current.get("execution", {}))
            execution["sse_reconnects"] = int(execution.get("sse_reconnects", 0)) + 1
            current["execution"] = execution
            current["execution_state"] = ExecutionState.RUNNING.value
            current["phase"] = Phase.RUNNING

        self.store.update(task_id, mark_resumed)
        try:
            outcome = self._wait_for_events(
                task_id,
                client,
                session_id,
                timeout_seconds,
                lease,
                lambda: None,
                progress,
            )
        except OpenCodeError as error:
            return self._interrupted(
                task_id,
                client,
                session_id,
                "resume-connection-failed",
                str(error),
            )
        return self._record_outcome(task_id, client, session_id, outcome)

    def reply_and_wait(
        self,
        task_id: str,
        kind: str,
        payload: dict,
        timeout_seconds: int,
        lease: WaitLease,
        progress: Callable[[str], None] | None = None,
    ) -> dict:
        if lease.task_id != task_id:
            raise ValueError("wait lease task_id does not match reply task")
        state = self.store.load(task_id)
        client = self._client(state)
        session_id = state["opencode"]["session_id"]
        request = self._request(task_id)

        if kind == "review":
            if state["phase"] != Phase.REVIEWING:
                raise ValueError(f"cannot send review in phase {state['phase']}")
            review_round = int(state["execution"].get("review_round", 0)) + 1
            if review_round > 2:
                raise ValueError("maximum review rounds exceeded")

            def prepare_reply(current: dict) -> None:
                current["execution"]["review_round"] = review_round
                current["review_state"] = ReviewState.REVISION_REQUESTED.value
                current["phase"] = Phase.REVISION_REQUESTED

            self.store.update(task_id, prepare_reply)

            def send_reply() -> None:
                self.store.update(
                    task_id,
                    lambda current: current.update(
                        {"execution_state": ExecutionState.RUNNING.value, "phase": Phase.RUNNING}
                    ),
                )
                text = (
                    f"{state['execution']['dispatch_marker']} review round {review_round}\n"
                    f"{payload['text']}"
                )
                client.prompt_async(session_id, text, **self._prompt_options(request))

        elif kind == "continue":
            if (
                state["execution_state"] != ExecutionState.RUNNING.value
                or state["phase"] != Phase.PAUSED
            ):
                raise ValueError(
                    f"cannot continue session in phase {state['phase']}"
                )
            if state.get("opencode", {}).get("dispatch_state") != "SENT":
                raise ValueError("cannot continue before initial dispatch is confirmed")
            previous = (state.get("execution") or {}).get("continuation")
            if isinstance(previous, dict) and previous.get("dispatch_state") in {
                "SENDING",
                "UNCERTAIN",
            }:
                raise ValueError(
                    "cannot continue while a prior continuation is unresolved; use resume_wait"
                )

            snapshot = self._progress_service._progress_snapshot(
                state,
                client,
                session_id,
                persist=True,
            )
            if snapshot.get("diagnostic_error") is not None:
                raise ValueError(
                    "cannot continue because OpenCode session status is unavailable"
                )
            if snapshot.get("pending_permissions") or snapshot.get("pending_questions"):
                raise ValueError("cannot continue while OpenCode input is pending")
            if snapshot.get("pending_tools"):
                raise ValueError("cannot continue while an OpenCode tool is pending")
            if snapshot.get("session_status") is not None:
                raise ValueError(
                    "cannot continue while the OpenCode session is still busy; use resume_wait"
                )

            execution = state.get("execution") or {}
            continuation_round = int(execution.get("continuation_round", 0)) + 1
            marker = (
                f"{execution['dispatch_marker']} continuation {continuation_round}"
            )
            text_fingerprint = "sha256:" + hashlib.sha256(
                payload["text"].encode("utf-8")
            ).hexdigest()

            def send_reply() -> None:
                def mark_sending(current: dict) -> None:
                    current_execution = dict(current.get("execution") or {})
                    current_execution["continuation_round"] = continuation_round
                    current_execution["continuation"] = {
                        "round": continuation_round,
                        "marker": marker,
                        "text_fingerprint": text_fingerprint,
                        "dispatch_state": "SENDING",
                        "requested_at": utc_now(),
                    }
                    current["execution"] = current_execution
                    current["execution_state"] = ExecutionState.RUNNING.value
                    current["phase"] = Phase.RUNNING

                self.store.update(task_id, mark_sending)
                text = (
                    f"{marker}\n"
                    "Continue the same approved task without expanding its contract.\n"
                    f"{payload['text']}"
                )
                try:
                    client.prompt_async(
                        session_id,
                        text,
                        **self._prompt_options(request),
                    )
                except OpenCodeError:
                    def mark_uncertain(current: dict) -> None:
                        current_execution = dict(current.get("execution") or {})
                        current_continuation = dict(
                            current_execution.get("continuation") or {}
                        )
                        if current_continuation.get("marker") == marker:
                            current_continuation["dispatch_state"] = "UNCERTAIN"
                            current_continuation["uncertain_at"] = utc_now()
                            current_execution["continuation"] = current_continuation
                            current["execution"] = current_execution

                    self.store.update(task_id, mark_uncertain)
                    raise

                def mark_sent(current: dict) -> None:
                    current_execution = dict(current.get("execution") or {})
                    current_continuation = dict(
                        current_execution.get("continuation") or {}
                    )
                    if current_continuation.get("marker") == marker:
                        current_continuation["dispatch_state"] = "SENT"
                        current_continuation["sent_at"] = utc_now()
                        current_execution["continuation"] = current_continuation
                        current["execution"] = current_execution

                self.store.update(task_id, mark_sent)

        elif kind == "permission":
            if state["execution_state"] not in {
                ExecutionState.INPUT_REQUIRED.value,
                ExecutionState.RUNNING.value,
                ExecutionState.STALLED.value,
                ExecutionState.COMPLETED.value,
                ExecutionState.ABORTED.value,
            }:
                raise ValueError(f"cannot answer permission in phase {state['phase']}")
            permission, decision = self._pending_inputs._current_permission_for_reply(
                task_id,
                client,
                session_id,
                payload["request_id"],
            )
            if (
                payload["response"] != "reject"
                and decision.reason in SENSITIVE_PERMISSION_ASK_REASONS
            ):
                if payload.get("user_approved") is not True:
                    raise ValueError(
                        "high-risk permission reply requires user_approved=true"
                    )
                if not self._pending_inputs._approval_is_action_specific(
                    permission, payload.get("approval_basis")
                ):
                    raise ValueError(
                        "high-risk permission reply requires an action-specific approval_basis"
                    )
            if payload.get("remember_for_task") is True:
                if decision.action == "deny":
                    raise ValueError(
                        "task-local permission rules cannot override a policy denial"
                    )
                if payload["response"] != "once":
                    raise ValueError(
                        "task-local permission rules require response=once"
                    )
                if payload.get("user_approved") is not True or not self._pending_inputs._approval_is_action_specific(
                    permission,
                    payload.get("approval_basis"),
                ):
                    raise ValueError(
                        "task-local permission rules require explicit action-specific approval"
                    )
            if state["execution_state"] in {
                ExecutionState.COMPLETED.value,
                ExecutionState.ABORTED.value,
            }:
                state = self._progress_service._reopen_terminal_task(task_id)

            def send_reply() -> None:
                success = client.reply_permission(
                    session_id, payload["request_id"], payload["response"]
                )
                if success is False:
                    raise OpenCodeError("OpenCode rejected the permission reply")
                audit_entry = {
                    "request_id": payload["request_id"],
                    "permission": permission.get("permission"),
                    "decision": "user-reply",
                    "response": payload["response"],
                    "reason": decision.reason,
                    "user_approved": payload.get("user_approved") is True,
                    "approval_basis": payload.get("approval_basis"),
                    "remembered_for_task": payload.get("remember_for_task") is True,
                    "answered_at": utc_now(),
                }

                def record_reply(current: dict) -> None:
                    existing = current.get("permission_audit")
                    if not isinstance(existing, list):
                        existing = []
                    else:
                        existing = list(existing)
                    if not any(
                        isinstance(item, dict)
                        and item.get("request_id") == audit_entry["request_id"]
                        for item in existing
                    ):
                        existing.append(deepcopy(audit_entry))
                    current["permission_audit"] = existing
                    if payload.get("remember_for_task") is True:
                        self._pending_inputs._remember_task_permission(current, permission)
                    current["execution_state"] = ExecutionState.RUNNING.value
                    current["phase"] = Phase.RUNNING

                self.store.update(
                    task_id,
                    record_reply,
                )

        elif kind == "question":
            if state["execution_state"] not in {
                ExecutionState.INPUT_REQUIRED.value,
                ExecutionState.RUNNING.value,
                ExecutionState.STALLED.value,
                ExecutionState.COMPLETED.value,
                ExecutionState.ABORTED.value,
            }:
                raise ValueError(f"cannot answer question in phase {state['phase']}")
            self._pending_inputs._current_question_for_reply(
                task_id,
                client,
                session_id,
                payload["request_id"],
            )
            if state["execution_state"] in {
                ExecutionState.COMPLETED.value,
                ExecutionState.ABORTED.value,
            }:
                state = self._progress_service._reopen_terminal_task(task_id)

            def send_reply() -> None:
                self.store.update(
                    task_id,
                    lambda current: current.update(
                        {"execution_state": ExecutionState.RUNNING.value, "phase": Phase.RUNNING}
                    ),
                )
                client.reply_question(payload["request_id"], payload["answers"])

        else:
            raise ValueError(f"unsupported reply kind: {kind}")

        try:
            outcome = self._wait_for_events(
                task_id,
                client,
                session_id,
                timeout_seconds,
                lease,
                send_reply,
                progress,
            )
        except OpenCodeError as error:
            return self._interrupted(
                task_id,
                client,
                session_id,
                "reply-connection-failed",
                str(error),
            )
        return self._record_outcome(task_id, client, session_id, outcome)

    def status(self, task_id: str) -> dict:
        state = self.store.load(task_id)
        session_id = state.get("opencode", {}).get("session_id")
        if not session_id:
            return state
        try:
            progress = self._progress_service._progress_snapshot(
                state,
                self._client(state),
                session_id,
                persist=False,
            )
        except Exception as error:
            progress = deepcopy(state.get("progress") or {})
            progress["diagnostic_error"] = self._safe_diagnostic(error)
        return self._progress_service._project_live_state(state, progress)

    def read_transcript(
        self,
        task_id: str,
        cursor: str | None = None,
        limit: int = 20,
        include_tool_output: bool = False,
    ) -> dict:
        return self._result_service.read_transcript(
            task_id, cursor, limit, include_tool_output
        )

    def collect_result(self, task_id: str, review_evidence: dict | None = None) -> dict:
        return self._result_service.collect_result(task_id, review_evidence)

    def approve_review(self, task_id: str, payload: dict) -> dict:
        return self._result_service.approve_review(task_id, payload)

    def abort_task(self, task_id: str) -> dict:
        state = self.store.load(task_id)
        if state["execution_state"] == ExecutionState.ABORTED.value:
            return self._result(state, "ABORTED", reason="already-aborted")

        def record_intent(current: dict) -> None:
            current["abort"] = {"state": "REQUESTED", "requested_at": utc_now()}

        state = self.store.update(task_id, record_intent)
        self.wait_coordinator.cancel_task(task_id, "abort-task")
        session_id = state.get("opencode", {}).get("session_id")
        try:
            if session_id:
                self._client(state).abort(session_id)
        except OpenCodeError as error:
            diagnostic = self._safe_diagnostic(error)
            diagnostic_message = diagnostic.get("message", "OpenCode request failed")
            public_diagnostic = {
                key: diagnostic[key]
                for key in ("status", "path", "message")
                if key in diagnostic
            }

            def record_failure(current: dict) -> None:
                current["abort"] = {
                    "state": "FAILED",
                    "requested_at": current.get("abort", {}).get("requested_at"),
                    "message": diagnostic_message,
                }

            failed = self.store.update(task_id, record_failure)
            return self._result(
                failed,
                "FAILED",
                raw_outcome="abort_error",
                reason="abort-failed",
                error={"kind": "abort", **public_diagnostic},
            )

        def record_aborted(current: dict) -> None:
            current["abort"] = {
                "state": "COMPLETED",
                "requested_at": current.get("abort", {}).get("requested_at"),
                "completed_at": utc_now(),
            }
            current["execution_state"] = ExecutionState.ABORTED.value
            current["wait_state"] = WaitState.CANCELLED.value
            current["phase"] = Phase.CANCELLED

        aborted = self.store.update(task_id, record_aborted)
        return self._result(aborted, "ABORTED", raw_outcome="aborted")
