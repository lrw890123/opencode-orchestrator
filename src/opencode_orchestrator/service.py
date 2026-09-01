from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import time
from typing import Callable

from .collector import (
    collect_git_evidence,
    last_assistant_text,
    normalize_messages,
    truncate_text,
    write_result,
)
from .event_stream import EventOutcome, wait_for_session
from .git_workspace import GitWorkspace
from .opencode_client import OpenCodeClient, OpenCodeError, OpenCodeSelectionError
from .pending_input import (
    PendingInputError,
    ReconciliationResult,
    normalize_permission_request,
    normalize_question_request,
    permission_event,
    question_event,
)
from .permission_policy import (
    evaluate_permission,
    normalize_permission_policy,
    normalize_progress_policy,
)
from .policy import classify_risk
from .progress import (
    idle_seconds,
    is_meaningful_progress,
    last_turn_finished,
    latest_message_progress_at,
    pending_tools,
)
from .task_state import (
    ExecutionState,
    Phase,
    ReviewState,
    TaskStore,
    WaitState,
    atomic_write_json,
    new_task_id,
    utc_now,
)
from .wait_coordinator import WaitCoordinator, WaitLease


REQUIRED_REQUEST_KEYS = {
    "goal",
    "non_goals",
    "approved_plan",
    "allowed_paths",
    "forbidden_actions",
    "acceptance_criteria",
    "test_commands",
    "risk",
    "user_approved",
}

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

    def _validate_request(self, request: dict) -> None:
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        missing = sorted(REQUIRED_REQUEST_KEYS - set(request))
        if missing:
            raise ValueError(f"request is missing required keys: {', '.join(missing)}")
        if not request["goal"] or not request["allowed_paths"]:
            raise ValueError("request goal and allowed_paths must be non-empty")
        model = request.get("model")
        if model is not None:
            if not isinstance(model, dict) or set(model) != {"providerID", "modelID"}:
                raise ValueError("request model must contain exactly providerID and modelID")
            if not all(isinstance(model[key], str) and model[key].strip() for key in model):
                raise ValueError("request model providerID and modelID must be non-empty strings")
        effort = request.get("effort")
        if not isinstance(effort, str) or not effort.strip():
            raise ValueError("request effort must be a non-empty string")
        # Keep direct service callers on the same normalized contract as the
        # MCP facade.  prepare_task fingerprints and persists this mutated
        # private copy, so the original caller remains untouched.
        request["permission_policy"] = normalize_permission_policy(
            request.get("permission_policy")
        )
        request["progress_policy"] = normalize_progress_policy(
            request.get("progress_policy")
        )

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

    @staticmethod
    def _legacy_discovery_unsupported(error: OpenCodeError, legacy_path: str) -> bool:
        """Recognize only the exact legacy list route's HTTP 404 response."""

        if getattr(error, "status", None) == 404 and getattr(error, "path", None) == legacy_path:
            return True
        match = re.match(r"^OpenCode HTTP (?P<status>[0-9]{3}) for (?P<path>[^:]+):", str(error))
        return bool(
            match
            and match.group("status") == "404"
            and match.group("path").strip() == legacy_path
        )

    @classmethod
    def _pending_discovery_mode(cls, client, v2_template: str) -> str | None:
        """Return v2/legacy when the client exposes OpenAPI discovery metadata."""

        openapi = getattr(client, "openapi_paths", None)
        if not callable(openapi):
            return None
        paths = openapi()
        if not isinstance(paths, (set, list, tuple)):
            raise OpenCodeError("OpenCode returned an invalid OpenAPI path list")
        return "v2" if v2_template in paths else "legacy"

    @classmethod
    def _pending_fetch(
        cls,
        client,
        session_id: str,
        method: Callable,
        label: str,
        v2_template: str,
        legacy_path: str,
    ) -> list[dict]:
        mode = cls._pending_discovery_mode(client, v2_template)
        try:
            value = method(session_id)
        except OpenCodeError as error:
            if mode == "legacy" and cls._legacy_discovery_unsupported(error, legacy_path):
                return []
            raise
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise OpenCodeError(f"OpenCode returned an invalid {label} list")
        return value

    @classmethod
    def _safe_permission_projection(
        cls, raw: object, session_id: str
    ) -> tuple[dict | None, dict | None, bool]:
        """Normalize one permission or retain only safe identity fields.

        The third return value records whether the normalized request can be
        evaluated.  A malformed request with a safe id/session is still
        surfaced as ``ask``; a request whose ownership is not safe to expose
        is ignored and can never be auto-answered.
        """

        try:
            normalized = normalize_permission_request(raw, session_id)
            return normalized, permission_event(normalized), True
        except PendingInputError:
            if not isinstance(raw, dict):
                return None, None, False
            request_id = raw.get("id")
            actual_session = raw.get("sessionID")
            if not (
                cls._safe_identity(request_id)
                and cls._safe_identity(actual_session)
                and actual_session == session_id
            ):
                return None, None, False
            action = raw.get("action", raw.get("permission"))
            resources = raw.get("resources", raw.get("patterns"))
            properties: dict = {
                "id": request_id,
                "sessionID": actual_session,
            }
            if cls._safe_identity(action):
                properties["action"] = action
            if isinstance(resources, list) and all(
                isinstance(item, str) and item == item.strip() and item for item in resources
            ):
                properties["resources"] = list(resources)
            safe_resources = (
                list(resources)
                if isinstance(resources, list)
                and all(
                    isinstance(item, str) and item == item.strip() and item
                    for item in resources
                )
                else []
            )
            fallback = {
                "request_id": request_id,
                "session_id": actual_session,
                "permission": action if cls._safe_identity(action) else "",
                "patterns": safe_resources,
                "metadata": {},
                "message_id": None,
                "call_id": None,
                "_invalid": True,
            }
            return fallback, {"type": "permission.reconciled", "properties": properties}, False

    @classmethod
    def _safe_question_projection(
        cls, raw: object, session_id: str
    ) -> tuple[dict | None, dict | None, bool]:
        try:
            normalized = normalize_question_request(raw, session_id)
            return normalized, question_event(normalized), True
        except PendingInputError:
            if not isinstance(raw, dict):
                return None, None, False
            request_id = raw.get("id")
            actual_session = raw.get("sessionID")
            if not (
                cls._safe_identity(request_id)
                and cls._safe_identity(actual_session)
                and actual_session == session_id
            ):
                return None, None, False
            return (
                {
                    "request_id": request_id,
                    "session_id": actual_session,
                    "questions": [],
                    "_invalid": True,
                },
                {
                    "type": "question.reconciled",
                    "properties": {"id": request_id, "sessionID": actual_session},
                },
                False,
            )

    @classmethod
    def _required_permission_projection(
        cls, raw: object, session_id: str
    ) -> tuple[dict, dict, bool]:
        normalized, event, valid = cls._safe_permission_projection(raw, session_id)
        if normalized is None or event is None:
            raise OpenCodeError(
                "OpenCode returned a malformed session-scoped permission entry"
            )
        return normalized, event, valid

    @classmethod
    def _required_question_projection(
        cls, raw: object, session_id: str
    ) -> tuple[dict, dict, bool]:
        normalized, event, valid = cls._safe_question_projection(raw, session_id)
        if normalized is None or event is None:
            raise OpenCodeError(
                "OpenCode returned a malformed session-scoped question entry"
            )
        return normalized, event, valid

    @staticmethod
    def _visible_permission(request: dict) -> dict:
        return {
            key: deepcopy(request.get(key))
            for key in (
                "request_id",
                "session_id",
                "permission",
                "patterns",
                "metadata",
                "message_id",
                "call_id",
            )
            if key in request
        }

    @staticmethod
    def _visible_question(request: dict) -> dict:
        return {
            "request_id": request["request_id"],
            "session_id": request["session_id"],
            "questions": deepcopy(request.get("questions", [])),
        }

    def _record_pending_probe(
        self,
        task_id: str,
        pending_permissions: list[dict],
        pending_questions: list[dict],
        *,
        diagnostic: dict | None = None,
    ) -> None:
        def persist(state: dict) -> None:
            progress = state.get("progress")
            if not isinstance(progress, dict):
                progress = {}
            else:
                progress = dict(progress)
            progress["pending_permissions"] = [
                self._visible_permission(item) for item in pending_permissions
            ]
            progress["pending_tools"] = self._mark_permission_waits(
                progress.get("pending_tools"),
                progress["pending_permissions"],
            )
            progress["pending_questions"] = [
                self._visible_question(item) for item in pending_questions
            ]
            progress["last_input_probe_at"] = utc_now()
            if diagnostic is not None:
                progress["diagnostic_error"] = deepcopy(diagnostic)
            else:
                progress["diagnostic_error"] = None
            state["progress"] = progress

        self.store.update(task_id, persist)

    @staticmethod
    def _mark_permission_waits(tools: object, permissions: object) -> list[dict]:
        """Distinguish pre-execution tool parts blocked on a permission reply."""

        if not isinstance(tools, list):
            return []
        request_by_call: dict[str, str] = {}
        if isinstance(permissions, list):
            for permission in permissions:
                if not isinstance(permission, dict):
                    continue
                call_id = permission.get("call_id")
                request_id = permission.get("request_id")
                if isinstance(call_id, str) and isinstance(request_id, str):
                    request_by_call[call_id] = request_id
        projected = deepcopy(tools)
        for tool in projected:
            if not isinstance(tool, dict):
                continue
            request_id = request_by_call.get(tool.get("call_id"))
            if request_id is None:
                continue
            tool["status"] = "waiting_permission"
            tool["permission_request_id"] = request_id
        return projected

    def _record_probe_diagnostic(self, task_id: str, error: Exception) -> None:
        diagnostic = self._safe_diagnostic(error)

        def persist(state: dict) -> None:
            progress = state.get("progress")
            if not isinstance(progress, dict):
                progress = {}
            else:
                progress = dict(progress)
            progress["diagnostic_error"] = diagnostic
            state["progress"] = progress

        self.store.update(task_id, persist)

    def _input_probe_due(self, task_id: str) -> bool:
        """Return whether a heartbeat may trigger another input probe."""

        state = self.store.load(task_id)
        progress = state.get("progress") or {}
        policy = state.get("progress_policy") or {}
        interval = policy.get("input_probe_interval_seconds", 15)
        if not isinstance(interval, int) or isinstance(interval, bool) or interval < 0:
            interval = 15
        last = progress.get("last_input_probe_at")
        if not isinstance(last, str) or not last.strip():
            return True
        try:
            timestamp = datetime.fromisoformat(last.replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            timestamp = timestamp.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return True
        return (datetime.now(timezone.utc) - timestamp).total_seconds() >= interval

    @staticmethod
    def _reconciliation_failure() -> ReconciliationResult:
        return ReconciliationResult(
            EventOutcome(
                "disconnected",
                {"reason": "pending-input-probe-failed"},
                {},
            ),
            [],
            [],
            [],
        )

    def _handle_permission_request(
        self,
        task_id: str,
        client,
        session_id: str,
        permission: dict,
        event: dict,
        evaluable: bool,
        *,
        allow_reply: bool = True,
    ) -> tuple[str, EventOutcome | None, dict | None]:
        request_id = permission.get("request_id")
        audit = self.store.load(task_id).get("permission_audit")
        if not isinstance(audit, list):
            audit = []
        if any(
            isinstance(item, dict) and item.get("request_id") == request_id
            for item in audit
        ):
            return "duplicate", None, None
        if not evaluable:
            return "input", EventOutcome("permission", event, {}), None

        state = self.store.load(task_id)
        request = self._request(task_id)
        contract = request if isinstance(request, dict) else {}
        worktree = (state.get("worktree") or {}).get("path")
        try:
            decision = self._permission_decision(
                state,
                permission,
                contract,
                worktree,
            )
        except Exception:
            decision = None
        if decision is None or decision.action == "ask" or decision.response is None:
            return "input", EventOutcome("permission", event, {}), None
        if not allow_reply:
            return "input", EventOutcome("permission", event, {}), None

        permission_reply = getattr(client, "reply_permission", None)
        if not callable(permission_reply):
            self._record_probe_diagnostic(task_id, OpenCodeError("permission reply unavailable"))
            return "failed", self._reconciliation_failure().outcome, None
        try:
            success = permission_reply(session_id, request_id, decision.response)
            if success is False:
                raise OpenCodeError(f"OpenCode rejected permission reply {request_id}")
        except Exception as error:
            self._record_probe_diagnostic(task_id, error)
            return "failed", self._reconciliation_failure().outcome, None

        audit_entry = {
            "request_id": request_id,
            "permission": permission["permission"],
            "decision": decision.action,
            "response": decision.response,
            "reason": decision.reason,
            "answered_at": utc_now(),
        }

        def append_audit(current: dict) -> None:
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

        self.store.update(task_id, append_audit)
        return "answered", None, audit_entry

    def _handle_native_permission_event(
        self,
        task_id: str,
        client,
        session_id: str,
        event: dict,
        counters: dict[str, int],
    ) -> EventOutcome:
        properties = event.get("properties") if isinstance(event, dict) else None
        permission, _projected, evaluable = self._safe_permission_projection(
            properties,
            session_id,
        )
        if permission is None:
            return EventOutcome("permission", event, counters)
        self._record_pending_probe(task_id, [permission], [])
        status, outcome, _audit_entry = self._handle_permission_request(
            task_id,
            client,
            session_id,
            permission,
            event,
            evaluable,
        )
        if outcome is not None:
            return EventOutcome(outcome.kind, outcome.event, counters)
        if status in {"answered", "duplicate"}:
            return EventOutcome(
                "reconnect",
                {
                    "type": "native-permission.reconciled",
                    "properties": {
                        "id": permission["request_id"],
                        "sessionID": session_id,
                    },
                },
                counters,
            )
        return EventOutcome("permission", event, counters)

    def _current_permission_for_reply(
        self,
        task_id: str,
        client,
        session_id: str,
        request_id: str,
    ) -> tuple[dict, object]:
        """Resolve and re-evaluate the exact permission currently shown."""

        raw_permissions = self._pending_fetch(
            client,
            session_id,
            getattr(client, "pending_permissions", lambda _session_id: []),
            "permission",
            "/api/session/{sessionID}/permission",
            "/permission",
        )
        candidates: list[dict] = []
        for raw in raw_permissions:
            normalized, _event, _valid = self._required_permission_projection(
                raw, session_id
            )
            candidates.append(normalized)
        permission = next(
            (
                item
                for item in candidates
                if item.get("request_id") == request_id
                and item.get("session_id") == session_id
            ),
            None,
        )
        state = self.store.load(task_id)
        if permission is None:
            last_event = (state.get("execution") or {}).get("last_event")
            properties = (
                last_event.get("properties")
                if isinstance(last_event, dict)
                else None
            )
            event_matches = (
                isinstance(properties, dict)
                and properties.get("id") == request_id
                and properties.get("sessionID") == session_id
            )
            persisted = (state.get("progress") or {}).get("pending_permissions")
            if event_matches and isinstance(persisted, list):
                permission = next(
                    (
                        item
                        for item in persisted
                        if isinstance(item, dict)
                        and item.get("request_id") == request_id
                        and item.get("session_id") == session_id
                    ),
                    None,
                )
        if permission is None:
            raise ValueError("request_id is not a current pending permission")

        request = self._request(task_id)
        worktree = (state.get("worktree") or {}).get("path")
        decision = self._permission_decision(
            state,
            permission,
            request,
            worktree,
        )
        return permission, decision

    def _current_question_for_reply(
        self,
        task_id: str,
        client,
        session_id: str,
        request_id: str,
    ) -> dict:
        """Resolve the exact question that is still pending for this session."""

        raw_questions = self._pending_fetch(
            client,
            session_id,
            getattr(client, "pending_questions", lambda _session_id: []),
            "question",
            "/api/session/{sessionID}/question",
            "/question",
        )
        for raw in raw_questions:
            normalized, _event, _valid = self._required_question_projection(
                raw,
                session_id,
            )
            if normalized.get("request_id") == request_id:
                return normalized
        state = self.store.load(task_id)
        last_event = (state.get("execution") or {}).get("last_event")
        properties = (
            last_event.get("properties") if isinstance(last_event, dict) else None
        )
        event_matches = (
            isinstance(properties, dict)
            and properties.get("id") == request_id
            and properties.get("sessionID") == session_id
        )
        persisted = (state.get("progress") or {}).get("pending_questions")
        if event_matches and isinstance(persisted, list):
            for item in persisted:
                if (
                    isinstance(item, dict)
                    and item.get("request_id") == request_id
                    and item.get("session_id") == session_id
                ):
                    return item
        raise ValueError("request_id is not a current pending question")

    @staticmethod
    def _task_rule_policy(state: dict) -> dict | None:
        rules = state.get("task_permission_rules")
        if not isinstance(rules, list) or not rules:
            return None
        return normalize_permission_policy(
            {
                "default": "ask",
                "persistence": "task",
                "rules": rules,
            }
        )

    def _permission_decision(
        self,
        state: dict,
        permission: dict,
        request: dict,
        worktree: str | None,
    ):
        policy = state.get("permission_policy") or request.get("permission_policy")
        base_decision = evaluate_permission(policy, permission, request, worktree)
        if base_decision.action == "deny":
            return base_decision
        task_policy = self._task_rule_policy(state)
        if task_policy is not None:
            task_decision = evaluate_permission(
                task_policy,
                permission,
                request,
                worktree,
            )
            if task_decision.action == "allow":
                return task_decision
        return base_decision

    @staticmethod
    def _remember_task_permission(current: dict, permission: dict) -> list[dict]:
        existing = current.get("task_permission_rules")
        rules = list(existing) if isinstance(existing, list) else []
        candidates = [
            {
                "permission": permission["permission"],
                "pattern": pattern,
                "action": "allow",
            }
            for pattern in permission.get("patterns", [])
        ]
        normalized = normalize_permission_policy(
            {
                "default": "ask",
                "persistence": "task",
                "rules": [*rules, *candidates],
            }
        )["rules"]
        deduplicated: list[dict] = []
        for rule in normalized:
            if rule not in deduplicated:
                deduplicated.append(rule)
        current["task_permission_rules"] = deduplicated
        return candidates

    @staticmethod
    def _approval_is_action_specific(permission: dict, basis: object) -> bool:
        if not isinstance(basis, str) or not basis.strip():
            return False
        lowered = basis.casefold()
        action = permission.get("permission")
        targets = permission.get("patterns")
        return (
            isinstance(action, str)
            and action.casefold() in lowered
            and isinstance(targets, list)
            and bool(targets)
            and all(
                isinstance(target, str) and target.casefold() in lowered
                for target in targets
            )
        )

    def _reconcile_pending_inputs(
        self, task_id: str, client, session_id: str
    ) -> ReconciliationResult:
        """Reconcile queued permissions/questions without creating a prompt.

        Safe task-local permissions are answered in order and re-fetched after
        each reply.  The first unsafe or malformed-but-identifiable request is
        surfaced to the caller, while all request projections persisted in the
        task remain metadata-free.
        """

        permission_fetch = getattr(client, "pending_permissions", None)
        question_fetch = getattr(client, "pending_questions", None)
        if permission_fetch is None:
            permission_fetch = lambda _session_id: []
        if question_fetch is None:
            question_fetch = lambda _session_id: []

        answered: list[dict] = []
        seen_ids: set[str] = set()
        replies = 0
        max_replies = 100

        while True:
            try:
                raw_permissions = self._pending_fetch(
                    client,
                    session_id,
                    permission_fetch,
                    "permission",
                    "/api/session/{sessionID}/permission",
                    "/permission",
                )
                raw_questions = self._pending_fetch(
                    client,
                    session_id,
                    question_fetch,
                    "question",
                    "/api/session/{sessionID}/question",
                    "/question",
                )
                permissions: list[dict] = []
                permission_events: list[dict] = []
                evaluable: list[bool] = []
                for raw in raw_permissions:
                    normalized, event, valid = self._required_permission_projection(
                        raw, session_id
                    )
                    permissions.append(normalized)
                    permission_events.append(event)
                    evaluable.append(valid)
                questions: list[dict] = []
                question_events: list[dict] = []
                for raw in raw_questions:
                    normalized, event, _valid = self._required_question_projection(
                        raw, session_id
                    )
                    questions.append(normalized)
                    question_events.append(event)
                self._record_pending_probe(task_id, permissions, questions)
            except Exception as error:
                self._record_probe_diagnostic(task_id, error)
                return self._reconciliation_failure()

            replied_this_pass = False
            for index, permission in enumerate(permissions):
                request_id = permission.get("request_id")
                if not self._safe_identity(request_id) or request_id in seen_ids:
                    continue
                seen_ids.add(request_id)
                status, permission_outcome, audit_entry = self._handle_permission_request(
                    task_id,
                    client,
                    session_id,
                    permission,
                    permission_events[index],
                    evaluable[index],
                    allow_reply=replies < max_replies,
                )
                if permission_outcome is not None:
                    return ReconciliationResult(
                        permission_outcome,
                        list(answered),
                        [self._visible_permission(item) for item in permissions[index:]],
                        [self._visible_question(item) for item in questions],
                    )
                if status == "duplicate":
                    continue
                if status != "answered" or audit_entry is None:
                    return self._reconciliation_failure()
                answered.append(audit_entry)
                replies += 1
                replied_this_pass = True
                break

            if replied_this_pass:
                continue

            if questions:
                return ReconciliationResult(
                    EventOutcome("question", question_events[0], {}),
                    list(answered),
                    [self._visible_permission(item) for item in permissions],
                    [self._visible_question(item) for item in questions],
                )
            return ReconciliationResult(
                None,
                list(answered),
                [self._visible_permission(item) for item in permissions],
                [self._visible_question(item) for item in questions],
            )

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
        progress.setdefault("last_turn_finished", False)

        def pending(
            method_name: str,
            label: str,
            v2_template: str,
            legacy_path: str,
        ) -> list[dict]:
            method = getattr(client, method_name, None)
            if not callable(method):
                return []
            return self._pending_fetch(
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
                normalized, _event, _valid = self._required_permission_projection(
                    raw, session_id
                )
                permissions.append(normalized)
            questions: list[dict] = []
            for raw in raw_questions:
                normalized, _event, _valid = self._required_question_projection(
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
            progress["last_turn_finished"] = last_turn_finished(messages)
            progress["pending_permissions"] = [
                self._visible_permission(item) for item in permissions
            ]
            progress["pending_tools"] = self._mark_permission_waits(
                pending_tools(messages),
                progress["pending_permissions"],
            )
            progress["pending_questions"] = [
                self._visible_question(item) for item in questions
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
        newer_progress = self._timestamp_is_newer(
            progress.get("last_progress_at"),
            persisted_progress.get("last_progress_at"),
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

    def _terminal_execution_state(self, task_id: str) -> str | None:
        """Return the persisted execution state when it is terminal."""

        state = self.store.load(task_id)
        if state["execution_state"] in {
            ExecutionState.COMPLETED.value,
            ExecutionState.ABORTED.value,
        }:
            return state["execution_state"]
        return None

    def _reconcile_finished_turn(
        self,
        task_id: str,
        state: dict,
        session_id: str | None,
    ) -> dict:
        """Adopt durable transcript evidence that a running task's turn ended.

        A session driven from another OpenCode process (or a runtime whose
        ``busy`` flag outlived its turn) never emits ``session.idle`` on this
        process's event stream, so the task can stay RUNNING forever.  When
        the transcript ends on a completed assistant turn and no input is
        pending, that transcript evidence is authoritative: persist the
        transition to COMPLETED/COLLECTING and let collection proceed.
        """

        if state["execution_state"] not in {
            ExecutionState.RUNNING.value,
            ExecutionState.STALLED.value,
        } or not session_id:
            return state
        if state.get("opencode", {}).get("dispatch_state") != "SENT":
            return state
        client = self._client(state)
        live_progress = self._progress_snapshot(
            state,
            client,
            session_id,
            persist=False,
        )
        if live_progress.get("diagnostic_error") is not None:
            return state
        if (
            live_progress.get("pending_permissions")
            or live_progress.get("pending_questions")
            or live_progress.get("pending_tools")
        ):
            return state
        if live_progress.get("last_turn_finished") is not True:
            return state
        return self._persist_terminal_reentry(
            task_id,
            live_progress,
            (
                ExecutionState.COMPLETED.value,
                Phase.COLLECTING,
                ReviewState.READY.value,
            ),
        )

    def _reentry_probe_due(self, state: dict) -> bool:
        """Return whether a terminal-reentry activity probe may run now."""

        progress = state.get("progress") or {}
        policy = state.get("progress_policy") or {}
        interval = policy.get("input_probe_interval_seconds", 15)
        if not isinstance(interval, int) or isinstance(interval, bool) or interval < 0:
            interval = 15
        last = progress.get("last_reentry_probe_at")
        if not isinstance(last, str) or not last.strip():
            return True
        try:
            timestamp = datetime.fromisoformat(last.replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            timestamp = timestamp.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return True
        return (datetime.now(timezone.utc) - timestamp).total_seconds() >= interval

    def _adopt_terminal_activity(
        self,
        task_id: str,
        client,
        session_id: str,
        event: dict,
    ) -> None:
        """Reopen a terminal task when live session activity re-enters it."""

        if not is_meaningful_progress(event, session_id):
            return
        event_type = event.get("type") if isinstance(event, dict) else None
        low_frequency = event_type in {"session.idle", "session.error"} or (
            isinstance(event_type, str)
            and (
                event_type.startswith("permission.")
                or event_type.startswith("question.")
            )
        )
        state = self.store.load(task_id)
        if state["execution_state"] not in {
            ExecutionState.COMPLETED.value,
            ExecutionState.ABORTED.value,
        }:
            return
        if not low_frequency and not self._reentry_probe_due(state):
            return
        if not low_frequency:
            def mark_probe(current: dict) -> None:
                progress = dict(current.get("progress") or {})
                progress["last_reentry_probe_at"] = utc_now()
                current["progress"] = progress

            state = self.store.update(task_id, mark_probe)
        live_progress = self._progress_snapshot(
            state,
            client,
            session_id,
            persist=False,
        )
        if self._terminal_live_projection(state, live_progress) is None:
            return
        self._reopen_terminal_task(task_id, live_progress)

    def _wait_for_terminal_reentry(
        self,
        task_id: str,
        client,
        session_id: str,
        timeout_seconds: int,
        lease: WaitLease,
        progress: Callable[[str], None] | None = None,
    ) -> dict:
        """Block on a terminal task's session until external activity re-enters.

        The wait stays attached without mutating the terminal record.  When a
        live session event re-enters the session, the task is reopened first
        so the same event stream records its outcome on the reacquired task.
        """

        try:
            outcome = self._wait_for_events(
                task_id,
                client,
                session_id,
                timeout_seconds,
                lease,
                lambda: None,
                progress,
                adopt_terminal=True,
            )
        except OpenCodeError as error:
            if self._terminal_execution_state(task_id) is not None:
                state = self.store.load(task_id)
                return self._current_result(state)
            return self._interrupted(
                task_id,
                client,
                session_id,
                "resume-connection-failed",
                str(error),
            )
        if self._terminal_execution_state(task_id) is not None:
            state = self.store.load(task_id)
            if outcome.kind in {"permission", "question"}:
                live_progress = self._progress_snapshot(
                    state,
                    client,
                    session_id,
                    persist=False,
                )
                if self._terminal_live_projection(state, live_progress) is not None:
                    self._reopen_terminal_task(task_id, live_progress)
                    return self._record_outcome(task_id, client, session_id, outcome)
            if outcome.kind == "cancelled":
                return self._result(
                    state,
                    "WAIT_CANCELLED",
                    raw_outcome="cancelled",
                    reason="current-state",
                )
            return self._current_result(state)
        return self._record_outcome(task_id, client, session_id, outcome)

    def _project_live_state(self, state: dict, progress: dict) -> dict:
        refreshed = deepcopy(state)
        refreshed_progress = deepcopy(progress)
        projection = self._terminal_live_projection(state, refreshed_progress)
        refreshed_progress["external_activity_detected"] = projection is not None
        refreshed["progress"] = refreshed_progress
        if projection is not None:
            (
                refreshed["execution_state"],
                refreshed["phase"],
                refreshed["review_state"],
            ) = projection
        return refreshed

    def _persist_terminal_reentry(
        self,
        task_id: str,
        progress: dict | None,
        target: tuple[str, str, str],
    ) -> dict:
        """Adopt externally resumed work after an explicit task operation."""

        detected_at = utc_now()

        def reopen(current: dict) -> None:
            if current.get("execution_state") not in {
                ExecutionState.COMPLETED.value,
                ExecutionState.ABORTED.value,
                ExecutionState.RUNNING.value,
                ExecutionState.STALLED.value,
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
            (
                current["execution_state"],
                current["phase"],
                current["review_state"],
            ) = target
            if progress is not None:
                current_progress = deepcopy(progress)
                current_progress["external_activity_detected"] = True
                current["progress"] = current_progress

        return self.store.update(task_id, reopen)

    def _reopen_terminal_task(self, task_id: str, progress: dict | None = None) -> dict:
        """Persist a running transition for externally resumed session work."""

        return self._persist_terminal_reentry(
            task_id,
            progress,
            (
                ExecutionState.RUNNING.value,
                Phase.RUNNING,
                ReviewState.REVISION_REQUESTED.value,
            ),
        )

    def _preflight_wait(self, task_id: str, client, session_id: str) -> EventOutcome | None:
        """Reconcile pending input and decide whether SSE waiting may proceed."""

        reconciliation = self._reconcile_pending_inputs(task_id, client, session_id)
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
        threshold = policy.get("stall_timeout_seconds", 600)
        if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 0:
            threshold = 600
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
    def _task_fingerprint(base_sha: str, request: dict) -> str:
        canonical = json.dumps(
            {"base_sha": base_sha, "request": request},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(canonical).hexdigest()}"

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
            "schema_version": 3,
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
        request = deepcopy(request)
        request.setdefault("effort", "max")
        self._validate_request(request)
        workspace = GitWorkspace(repo)
        facts = workspace.facts()
        fingerprint = self._task_fingerprint(facts.head_sha, request)
        task_id = new_task_id()
        self.store.create(
            task_id,
            str(facts.repo_root),
            facts.head_sha,
            facts.branch,
            facts.dirty_fingerprint,
        )
        atomic_write_json(self.store.task_dir(task_id) / "request.json", request)
        risk = classify_risk(**request["risk"])
        policy = {
            "risk": risk.level,
            "reasons": list(risk.reasons),
            "user_approval_required": risk.user_approval_required,
            "user_approved": bool(request["user_approved"]),
            "allowed_paths": list(request["allowed_paths"]),
        }

        def record_policy(state: dict) -> None:
            state["task_fingerprint"] = fingerprint
            state["policy"] = policy
            state["permission_policy"] = deepcopy(request["permission_policy"])
            state["progress_policy"] = deepcopy(request["progress_policy"])
            state["slug"] = slug
            state["phase"] = Phase.RISK_CHECK
            state["opencode"] = {
                "base_url": server_url,
                "requested_model": deepcopy(request.get("model")),
                "effort": request["effort"],
                "dispatch_marker": state["execution"]["dispatch_marker"],
                "dispatch_state": "NOT_STARTED",
                "dispatch_retry_count": 0,
            }

        self.store.update(task_id, record_policy)
        if risk.user_approval_required and not request["user_approved"]:
            return self.store.update(
                task_id,
                lambda current: current.update({"phase": Phase.AWAITING_APPROVAL}),
            )

        self.store.update(task_id, lambda current: current.update({"phase": Phase.PREPARING}))
        try:
            prepared = workspace.prepare(self.state_root, task_id, slug)
        except Exception as error:
            def fail(current: dict) -> None:
                current["phase"] = Phase.FAILED
                current["execution_state"] = ExecutionState.FAILED.value
                current["failure"] = {"message": str(error)}

            self.store.update(task_id, fail)
            raise

        def record_workspace(current: dict) -> None:
            current["worktree"] = {
                "path": str(prepared.path),
                "branch": prepared.branch,
                "base_sha": prepared.base_sha,
            }
            current["opencode"]["directory"] = str(prepared.path)

        return self.store.update(task_id, record_workspace)

    def prepare(
        self,
        repo: Path,
        slug: str,
        request: dict,
        server_url: str = "http://127.0.0.1:4096",
    ) -> dict:
        return self.prepare_task(repo, slug, request, server_url)

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
        adopt_terminal: bool = False,
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
            if adopt_terminal:
                self._adopt_terminal_activity(task_id, client, session_id, event)
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
                return self._handle_native_permission_event(
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
            if (
                event_type == "server.heartbeat"
                and self._input_probe_due(task_id)
                and not (
                    adopt_terminal
                    and self._terminal_execution_state(task_id) is not None
                )
            ):
                before = self.store.load(task_id).get("permission_audit") or []
                before_count = len(before) if isinstance(before, list) else 0
                preflight = self._preflight_wait(task_id, client, session_id)
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
                reconciliation = self._reconcile_pending_inputs(task_id, client, session_id)
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
            reconciliation = self._reconcile_pending_inputs(task_id, client, session_id)
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
            live_progress = self._progress_snapshot(
                state,
                client,
                session_id,
                persist=False,
            )
            if self._terminal_live_projection(state, live_progress) is None:
                return self._wait_for_terminal_reentry(
                    task_id,
                    client,
                    session_id,
                    timeout_seconds,
                    lease,
                    progress,
                )
            state = self._reopen_terminal_task(task_id, live_progress)

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

        preflight = self._preflight_wait(task_id, client, session_id)
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
            terminal_reacquire = state["execution_state"] in {
                ExecutionState.COMPLETED.value,
                ExecutionState.ABORTED.value,
            }
            if terminal_reacquire:
                if payload.get("reacquire") is not True:
                    raise ValueError(
                        "cannot continue a completed or aborted task unless "
                        "payload.reacquire=true confirms re-acquisition of its session"
                    )
                if not session_id:
                    raise ValueError(
                        "cannot continue a terminal task without an OpenCode session"
                    )
                if (state.get("abort") or {}).get("state") == "REQUESTED":
                    raise ValueError(
                        "cannot continue while an abort request is still in progress"
                    )
            elif state["execution_state"] == ExecutionState.STALLED.value:
                if state.get("phase") != Phase.STALLED:
                    raise ValueError(
                        f"cannot continue session in phase {state['phase']}"
                    )
            elif (
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

            snapshot = self._progress_snapshot(
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

            if terminal_reacquire:
                state = self._reopen_terminal_task(task_id)

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
            permission, decision = self._current_permission_for_reply(
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
                if not self._approval_is_action_specific(
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
                if payload.get("user_approved") is not True or not self._approval_is_action_specific(
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
                state = self._reopen_terminal_task(task_id)

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
                        self._remember_task_permission(current, permission)
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
            self._current_question_for_reply(
                task_id,
                client,
                session_id,
                payload["request_id"],
            )
            if state["execution_state"] in {
                ExecutionState.COMPLETED.value,
                ExecutionState.ABORTED.value,
            }:
                state = self._reopen_terminal_task(task_id)

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
            progress = self._progress_snapshot(
                state,
                self._client(state),
                session_id,
                persist=False,
            )
        except Exception as error:
            progress = deepcopy(state.get("progress") or {})
            progress["diagnostic_error"] = self._safe_diagnostic(error)
        return self._project_live_state(state, progress)

    def read_transcript(
        self,
        task_id: str,
        cursor: str | None = None,
        limit: int = 20,
        include_tool_output: bool = False,
    ) -> dict:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0 or limit > 100:
            raise ValueError("transcript limit must be between 1 and 100")
        try:
            start = 0 if cursor is None else int(cursor, 10)
        except (TypeError, ValueError) as error:
            raise ValueError("transcript cursor must be a decimal string") from error
        if start < 0 or (cursor is not None and str(start) != cursor):
            raise ValueError("transcript cursor must be a non-negative decimal string")
        state = self.store.load(task_id)
        session_id = state.get("opencode", {}).get("session_id")
        if not session_id:
            raise ValueError("task has no OpenCode session transcript")
        messages = self._client(state).messages(session_id, limit=10000)
        safe_snapshot = normalize_messages(messages, include_tool_output=False)
        atomic_write_json(
            self.store.task_dir(task_id) / "transcript.json",
            {"task_id": task_id, "session_id": session_id, "messages": safe_snapshot},
        )
        normalized = (
            normalize_messages(messages, include_tool_output=True)
            if include_tool_output
            else safe_snapshot
        )
        page = normalized[start : start + limit]
        end = start + len(page)
        return {
            "schema_version": 3,
            "task_id": task_id,
            "session_id": session_id,
            "messages": page,
            "next_cursor": str(end) if end < len(normalized) else None,
        }

    def collect_result(self, task_id: str, review_evidence: dict | None = None) -> dict:
        state = self.store.load(task_id)
        client = None
        session_id = state.get("opencode", {}).get("session_id")
        if (
            state["execution_state"]
            in {ExecutionState.COMPLETED.value, ExecutionState.ABORTED.value}
            and session_id
        ):
            client = self._client(state)
            live_progress = self._progress_snapshot(
                state,
                client,
                session_id,
                persist=False,
            )
            projection = self._terminal_live_projection(state, live_progress)
            if projection is not None:
                projected_execution = projection[0]
                if projected_execution != ExecutionState.COMPLETED.value:
                    next_action = (
                        "reply_and_wait"
                        if projected_execution == ExecutionState.INPUT_REQUIRED.value
                        else "resume_wait"
                    )
                    raise ValueError(
                        "cannot collect while external session is "
                        f"{projected_execution}; use {next_action}"
                    )
                state = self._persist_terminal_reentry(
                    task_id,
                    live_progress,
                    projection,
                )
        if state["execution_state"] != ExecutionState.COMPLETED.value:
            state = self._reconcile_finished_turn(task_id, state, session_id)
        if state["execution_state"] != ExecutionState.COMPLETED.value:
            raise ValueError(
                f"cannot collect task in execution state {state['execution_state']}"
            )
        client = client or self._client(state)
        session_id = state["opencode"]["session_id"]
        assistant_full = last_assistant_text(client.messages(session_id, limit=10000))
        assistant_result, truncated = truncate_text(assistant_full)
        evidence = collect_git_evidence(
            Path(state["worktree"]["path"]),
            state["source"]["base_sha"],
            state["policy"]["allowed_paths"],
        )
        source_warning = (
            GitWorkspace(Path(state["source"]["repo_root"])).dirty_fingerprint()
            != state["source"]["dirty_fingerprint"]
        )
        result = {
            "schema_version": 3,
            "ok": not evidence["out_of_scope"],
            "task_id": task_id,
            "session_id": session_id,
            "assistant_result": assistant_result,
            "assistant_result_truncated": truncated,
            "opencode_diff": client.session_diff(session_id),
            "source_fingerprint_warning": source_warning,
            "poll_fallback_used": state["execution"].get("poll_fallback_used", False),
            **evidence,
        }
        if review_evidence is not None:
            result["review_evidence"] = deepcopy(review_evidence)
        write_result(self.store.task_dir(task_id), result)

        def mark_collected(current: dict) -> None:
            if evidence["out_of_scope"]:
                current["execution_state"] = ExecutionState.FAILED.value
                current["phase"] = Phase.FAILED
            else:
                current["review_state"] = ReviewState.REVIEWING.value
                current["phase"] = Phase.REVIEWING
                if review_evidence is not None:
                    current["review_evidence"] = deepcopy(review_evidence)

        updated = self.store.update(task_id, mark_collected)
        result["phase"] = updated["phase"]
        result["execution_state"] = updated["execution_state"]
        result["review_state"] = updated["review_state"]
        return result

    def approve_review(self, task_id: str, payload: dict) -> dict:
        state = self.store.load(task_id)
        if state["phase"] != Phase.REVIEWING:
            raise ValueError(f"cannot approve review in phase {state['phase']}")
        if payload.get("tests_passed") is not True:
            raise ValueError("review tests_passed must be true")
        summary = payload.get("review_summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("review_summary must be a non-empty string")
        review = deepcopy(payload)
        review["approved_at"] = utc_now()

        def approve(current: dict) -> None:
            current["review"] = review
            current["review_state"] = ReviewState.AWAITING_INTEGRATION.value
            current["phase"] = Phase.AWAITING_INTEGRATION

        return self.store.update(task_id, approve)

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

    @staticmethod
    def _compat_wait_result(result: dict) -> dict:
        compatible = dict(result)
        raw = result.get("raw_outcome")
        if raw == "configuration_error":
            legacy_outcome = "configuration_error"
        elif raw in OUTCOME_TO_STATE:
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

    def dispatch(self, task_id: str, timeout_seconds: int = 1800) -> dict:
        request_id = self._request_id("compat-dispatch", task_id)
        with self.wait_coordinator.attach(task_id, request_id) as lease:
            return self._compat_wait_result(
                self.dispatch_and_wait(task_id, timeout_seconds, lease)
            )

    def wait(self, task_id: str, timeout_seconds: int = 1800) -> dict:
        request_id = self._request_id("compat-resume", task_id)
        with self.wait_coordinator.attach(task_id, request_id) as lease:
            return self._compat_wait_result(
                self.resume_wait(task_id, timeout_seconds, lease)
            )

    def reply(
        self,
        task_id: str,
        kind: str,
        payload: dict,
        timeout_seconds: int = 1800,
    ) -> dict:
        request_id = self._request_id("compat-reply", task_id)
        with self.wait_coordinator.attach(task_id, request_id) as lease:
            return self._compat_wait_result(
                self.reply_and_wait(task_id, kind, payload, timeout_seconds, lease)
            )

    def collect(self, task_id: str) -> dict:
        return self.collect_result(task_id)

    def abort(self, task_id: str) -> dict:
        return self.abort_task(task_id)

    def cleanup(self, task_id: str, confirm: str) -> dict:
        if confirm != task_id:
            raise ValueError("cleanup confirmation must equal the exact task id")
        with self.store.lock(task_id):
            state = self.store.load(task_id)
            worktree = Path(state["worktree"]["path"]).resolve()
            managed = (self.state_root / "worktrees").resolve()
            forbidden = {
                Path("/").resolve(),
                Path.home().resolve(),
                Path(state["source"]["repo_root"]).resolve(),
            }
            if worktree in forbidden or not worktree.is_relative_to(managed):
                raise ValueError(f"refusing cleanup outside managed worktrees: {worktree}")
            subprocess.run(
                ["git", "-C", state["source"]["repo_root"], "worktree", "remove", str(worktree)],
                check=True,
                text=True,
                capture_output=True,
            )
            task_dir = self.store.task_dir(task_id).resolve()
            if not task_dir.is_relative_to((self.state_root / "tasks").resolve()):
                raise ValueError(f"refusing task cleanup outside managed tasks: {task_dir}")
            shutil.rmtree(task_dir)
            return {"ok": True, "task_id": task_id, "cleaned": True}
