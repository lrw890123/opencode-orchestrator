from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import re
from typing import Callable

from .contracts import DEFAULT_INPUT_PROBE_SECONDS
from .event_stream import EventOutcome
from .opencode_client import OpenCodeError
from .pending_input import (
    PendingInputError,
    ReconciliationResult,
    normalize_permission_request,
    normalize_question_request,
    permission_event,
    question_event,
)
from .permission_policy import evaluate_permission, normalize_permission_policy
from .task_state import utc_now


class PendingInputCoordinator:
    """Own pending-input discovery, policy decisions, and audit projection."""

    def __init__(
        self,
        store,
        request_loader: Callable[[str], dict],
        diagnostic_projector: Callable[[Exception], dict],
        identity_validator: Callable[[object], bool],
    ) -> None:
        self.store = store
        self._request = request_loader
        self._safe_diagnostic = diagnostic_projector
        self._safe_identity = identity_validator

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

    def _pending_discovery_mode(self, client, v2_template: str) -> str | None:
        """Return v2/legacy when the client exposes OpenAPI discovery metadata."""

        openapi = getattr(client, "openapi_paths", None)
        if not callable(openapi):
            return None
        paths = openapi()
        if not isinstance(paths, (set, list, tuple)):
            raise OpenCodeError("OpenCode returned an invalid OpenAPI path list")
        return "v2" if v2_template in paths else "legacy"

    def _pending_fetch(
        self,
        client,
        session_id: str,
        method: Callable,
        label: str,
        v2_template: str,
        legacy_path: str,
    ) -> list[dict]:
        mode = self._pending_discovery_mode(client, v2_template)
        try:
            value = method(session_id)
        except OpenCodeError as error:
            if mode == "legacy" and self._legacy_discovery_unsupported(error, legacy_path):
                return []
            raise
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise OpenCodeError(f"OpenCode returned an invalid {label} list")
        return value

    def _safe_permission_projection(
        self, raw: object, session_id: str
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
                self._safe_identity(request_id)
                and self._safe_identity(actual_session)
                and actual_session == session_id
            ):
                return None, None, False
            action = raw.get("action", raw.get("permission"))
            resources = raw.get("resources", raw.get("patterns"))
            properties: dict = {
                "id": request_id,
                "sessionID": actual_session,
            }
            if self._safe_identity(action):
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
                "permission": action if self._safe_identity(action) else "",
                "patterns": safe_resources,
                "metadata": {},
                "message_id": None,
                "call_id": None,
                "_invalid": True,
            }
            return fallback, {"type": "permission.reconciled", "properties": properties}, False

    def _safe_question_projection(
        self, raw: object, session_id: str
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
                self._safe_identity(request_id)
                and self._safe_identity(actual_session)
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

    def _required_permission_projection(
        self, raw: object, session_id: str
    ) -> tuple[dict, dict, bool]:
        normalized, event, valid = self._safe_permission_projection(raw, session_id)
        if normalized is None or event is None:
            raise OpenCodeError(
                "OpenCode returned a malformed session-scoped permission entry"
            )
        return normalized, event, valid

    def _required_question_projection(
        self, raw: object, session_id: str
    ) -> tuple[dict, dict, bool]:
        normalized, event, valid = self._safe_question_projection(raw, session_id)
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
        interval = policy.get(
            "input_probe_interval_seconds", DEFAULT_INPUT_PROBE_SECONDS
        )
        if not isinstance(interval, int) or isinstance(interval, bool) or interval < 0:
            interval = DEFAULT_INPUT_PROBE_SECONDS
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
