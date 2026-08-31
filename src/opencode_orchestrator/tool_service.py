from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import re

from .permission_policy import normalize_permission_policy, normalize_progress_policy
from .tools import TOOL_DEFINITIONS


TOOL_NAMES = {definition["name"] for definition in TOOL_DEFINITIONS}
TASK_ID_PATTERN = re.compile(r"^oc-[A-Za-z0-9._-]+$")
TIMEOUT_MIN = 1
TIMEOUT_MAX = 86400


class ToolInputError(ValueError):
    """Raised when an MCP tool call fails deterministic input validation."""


class ToolService:
    def __init__(self, bridge, coordinator=None):
        self.bridge = bridge
        self.coordinator = coordinator or bridge.wait_coordinator

    @staticmethod
    def _object(value, label: str) -> dict:
        if not isinstance(value, dict):
            raise ToolInputError(f"{label} must be an object")
        return value

    @staticmethod
    def _keys(value: dict, *, required: set[str], allowed: set[str], label: str) -> None:
        missing = sorted(required - set(value))
        if missing:
            raise ToolInputError(f"{label} is missing required keys: {', '.join(missing)}")
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ToolInputError(f"{label} has unknown keys: {', '.join(unknown)}")

    @staticmethod
    def _nonblank(value, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ToolInputError(f"{label} must be a non-empty string")
        return value

    @staticmethod
    def _string_list(value, label: str, *, nonempty: bool = False) -> list[str]:
        if not isinstance(value, list) or (nonempty and not value):
            suffix = " non-empty" if nonempty else ""
            raise ToolInputError(f"{label} must be a{suffix} array of strings")
        if any(not isinstance(item, str) for item in value):
            raise ToolInputError(f"{label} must contain only strings")
        if nonempty and any(not item.strip() for item in value):
            raise ToolInputError(f"{label} must not contain blank paths")
        return value

    @staticmethod
    def _boolean(value, label: str) -> bool:
        if not isinstance(value, bool):
            raise ToolInputError(f"{label} must be a boolean")
        return value

    @staticmethod
    def _integer(value, label: str, minimum: int, maximum: int) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ToolInputError(f"{label} must be an integer")
        if value < minimum or value > maximum:
            raise ToolInputError(f"{label} must be between {minimum} and {maximum}")
        return value

    def _task_id(self, value) -> str:
        task_id = self._nonblank(value, "task_id")
        if not TASK_ID_PATTERN.fullmatch(task_id):
            raise ToolInputError("task_id has an invalid format")
        return task_id

    def _timeout(self, arguments: dict) -> int:
        return self._integer(
            arguments.get("timeout_seconds", 3600),
            "timeout_seconds",
            TIMEOUT_MIN,
            TIMEOUT_MAX,
        )

    @staticmethod
    def _reply_input_error(error: ValueError) -> str | None:
        message = str(error)
        if "current pending permission" in message:
            return "permission request is no longer pending; refresh task status before replying"
        if "current pending question" in message:
            return "question request is no longer pending; refresh task status before replying"
        if message.startswith("high-risk permission reply requires"):
            return (
                "sensitive permission approval requires user_approved=true and an "
                "action-specific approval_basis naming the permission and target"
            )
        if message.startswith("cannot continue"):
            return message
        if message.startswith("cannot send review in phase"):
            return "review reply requires collect_result to enter REVIEWING"
        if message.startswith("task-local permission rules require"):
            return message
        if message.startswith("cannot answer ") or message in {
            "maximum review rounds exceeded",
            "request_id does not match the current pending permission",
        }:
            return "reply is invalid for the current task state; refresh task status"
        return None

    def _model(self, value) -> dict:
        model = self._object(value, "model")
        self._keys(
            model,
            required={"providerID", "modelID"},
            allowed={"providerID", "modelID"},
            label="model",
        )
        self._nonblank(model["providerID"], "model.providerID")
        self._nonblank(model["modelID"], "model.modelID")
        return deepcopy(model)

    def _risk(self, value) -> dict:
        risk = self._object(value, "task_contract.risk")
        keys = {
            "file_count",
            "line_count",
            "cross_module",
            "public_interface",
            "dependency_change",
            "high_risk_actions",
        }
        self._keys(risk, required=keys, allowed=keys, label="task_contract.risk")
        self._integer(risk["file_count"], "task_contract.risk.file_count", 0, 10**9)
        self._integer(risk["line_count"], "task_contract.risk.line_count", 0, 10**12)
        for key in ("cross_module", "public_interface", "dependency_change"):
            self._boolean(risk[key], f"task_contract.risk.{key}")
        self._string_list(
            risk["high_risk_actions"],
            "task_contract.risk.high_risk_actions",
        )
        return deepcopy(risk)

    def _task_contract(self, value) -> dict:
        contract = self._object(value, "task_contract")
        keys = {
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
        self._keys(contract, required=keys, allowed=keys, label="task_contract")
        self._nonblank(contract["goal"], "task_contract.goal")
        self._string_list(contract["non_goals"], "task_contract.non_goals")
        self._string_list(contract["approved_plan"], "task_contract.approved_plan")
        self._string_list(
            contract["allowed_paths"],
            "task_contract.allowed_paths",
            nonempty=True,
        )
        self._string_list(contract["forbidden_actions"], "task_contract.forbidden_actions")
        self._string_list(contract["acceptance_criteria"], "task_contract.acceptance_criteria")
        self._string_list(contract["test_commands"], "task_contract.test_commands")
        self._risk(contract["risk"])
        self._boolean(contract["user_approved"], "task_contract.user_approved")
        return deepcopy(contract)

    def _review_evidence(self, value) -> dict:
        evidence = self._object(value, "review_evidence")
        keys = {"tests_passed", "review_summary"}
        self._keys(evidence, required=keys, allowed=keys, label="review_evidence")
        if evidence["tests_passed"] is not True:
            raise ToolInputError("review_evidence.tests_passed must be true")
        self._nonblank(evidence["review_summary"], "review_evidence.review_summary")
        return deepcopy(evidence)

    @staticmethod
    def _slug(goal: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", goal.lower()).strip("-")[:48]
        return slug or "opencode-task"

    @staticmethod
    def _state_outcome(state: dict) -> str:
        execution = state.get("execution_state")
        if execution == "COMPLETED":
            return "COMPLETED"
        if execution == "INPUT_REQUIRED" or state.get("phase") == "AWAITING_APPROVAL":
            return "INPUT_REQUIRED"
        if execution == "FAILED":
            return "FAILED"
        if execution == "ABORTED":
            return "ABORTED"
        if execution == "STALLED":
            return "STALLED"
        if state.get("wait_state") == "CANCELLED":
            return "WAIT_CANCELLED"
        return "INTERRUPTED"

    @staticmethod
    def _summary(outcome: str) -> str:
        return {
            "COMPLETED": "OpenCode task data is ready for review.",
            "INPUT_REQUIRED": "The task requires an explicit reply or approval.",
            "FAILED": "The OpenCode task failed.",
            "ABORTED": "The OpenCode task was explicitly aborted.",
            "WAIT_CANCELLED": "The local wait was cancelled; OpenCode was not aborted.",
            "INTERRUPTED": "The task is preserved and can be resumed.",
            "STALLED": "The task stopped making progress and needs stall diagnostics.",
        }[outcome]

    @staticmethod
    def _next_action(outcome: str) -> str:
        return {
            "COMPLETED": "review_result",
            "INPUT_REQUIRED": "reply_and_wait",
            "FAILED": "inspect_failure",
            "ABORTED": "inspect_partial_result",
            "WAIT_CANCELLED": "resume_wait",
            "INTERRUPTED": "resume_wait",
            "STALLED": "inspect_stall",
        }[outcome]

    def _common(
        self,
        state: dict,
        *,
        outcome: str | None = None,
        summary: str | None = None,
        next_action: str | None = None,
        artifacts: dict | None = None,
    ) -> dict:
        resolved_outcome = outcome or self._state_outcome(state)
        return {
            "schema_version": 3,
            "task_id": state["task_id"],
            "outcome": resolved_outcome,
            "execution_state": state["execution_state"],
            "wait_state": state["wait_state"],
            "review_state": state.get("review_state"),
            "opencode_session_id": state.get("opencode", {}).get("session_id"),
            "summary": summary or self._summary(resolved_outcome),
            "next_action": next_action or self._next_action(resolved_outcome),
            "artifacts": artifacts or {},
        }

    def _service_result(self, result: dict) -> dict:
        required = {
            "schema_version",
            "task_id",
            "outcome",
            "execution_state",
            "wait_state",
            "summary",
            "next_action",
            "artifacts",
        }
        if required.issubset(result):
            public = dict(result)
            public["schema_version"] = 3
            return public
        state = self.bridge.status(result["task_id"])
        return self._common(state, artifacts={"result": result})

    def _validate_reply_payload(self, kind: str, value) -> dict:
        payload = self._object(value, "payload")
        if kind in {"review", "continue"}:
            if kind == "continue":
                self._keys(
                    payload,
                    required={"text"},
                    allowed={"text", "reacquire"},
                    label="payload",
                )
                if "reacquire" in payload:
                    self._boolean(payload["reacquire"], "payload.reacquire")
            else:
                self._keys(payload, required={"text"}, allowed={"text"}, label="payload")
            self._nonblank(payload["text"], "payload.text")
        elif kind == "permission":
            allowed = {
                "request_id",
                "response",
                "user_approved",
                "approval_basis",
                "remember_for_task",
            }
            self._keys(
                payload,
                required={"request_id", "response"},
                allowed=allowed,
                label="payload",
            )
            self._nonblank(payload["request_id"], "payload.request_id")
            if payload["response"] not in {"once", "always", "reject"}:
                raise ToolInputError("payload.response must be once, always, or reject")
            if "user_approved" in payload:
                self._boolean(payload["user_approved"], "payload.user_approved")
            if "approval_basis" in payload:
                self._nonblank(payload["approval_basis"], "payload.approval_basis")
            if "remember_for_task" in payload:
                self._boolean(
                    payload["remember_for_task"],
                    "payload.remember_for_task",
                )
            if payload["response"] == "always":
                if payload.get("user_approved") is not True:
                    raise ToolInputError("always permission requires payload.user_approved=true")
                self._nonblank(payload.get("approval_basis"), "payload.approval_basis")
            if payload.get("remember_for_task") is True:
                if payload["response"] != "once":
                    raise ToolInputError(
                        "payload.remember_for_task requires payload.response=once"
                    )
                if payload.get("user_approved") is not True:
                    raise ToolInputError(
                        "payload.remember_for_task requires payload.user_approved=true"
                    )
                self._nonblank(payload.get("approval_basis"), "payload.approval_basis")
        elif kind == "question":
            self._keys(
                payload,
                required={"request_id", "answers"},
                allowed={"request_id", "answers"},
                label="payload",
            )
            self._nonblank(payload["request_id"], "payload.request_id")
            answers = payload["answers"]
            if not isinstance(answers, list) or any(
                not isinstance(answer, list)
                or any(not isinstance(item, str) for item in answer)
                for answer in answers
            ):
                raise ToolInputError("payload.answers must be an array of string arrays")
        else:
            raise ToolInputError(
                "kind must be review, continue, permission, or question"
            )
        return deepcopy(payload)

    def call(
        self,
        name: str,
        arguments: dict,
        request_id: str,
        progress=None,
    ) -> dict:
        if name not in TOOL_NAMES:
            raise ToolInputError(f"unknown tool: {name}")
        arguments = self._object(arguments, "arguments")
        self._nonblank(request_id, "request_id")

        if name == "delegate_and_wait":
            if progress is not None:
                progress("OpenCode task is starting")
            allowed = {
                "repo_path",
                "task_contract",
                "slug",
                "model",
                "effort",
                "timeout_seconds",
                "server_url",
                "permission_policy",
                "progress_policy",
            }
            self._keys(
                arguments,
                required={"repo_path", "task_contract"},
                allowed=allowed,
                label="arguments",
            )
            repo_path = Path(self._nonblank(arguments["repo_path"], "repo_path"))
            if not repo_path.is_absolute():
                raise ToolInputError("repo_path must be absolute")
            contract = self._task_contract(arguments["task_contract"])
            try:
                contract["permission_policy"] = normalize_permission_policy(
                    arguments.get("permission_policy")
                )
                contract["progress_policy"] = normalize_progress_policy(
                    arguments.get("progress_policy")
                )
            except ValueError as error:
                raise ToolInputError(str(error)) from error
            if "model" in arguments:
                contract["model"] = self._model(arguments["model"])
            effort = self._nonblank(arguments.get("effort", "max"), "effort")
            contract["effort"] = effort
            timeout = self._timeout(arguments)
            slug = arguments.get("slug", self._slug(contract["goal"]))
            self._nonblank(slug, "slug")
            server_url = arguments.get(
                "server_url",
                os.environ.get("OPENCODE_SERVER_URL", "http://127.0.0.1:4096"),
            )
            self._nonblank(server_url, "server_url")
            prepared = self.bridge.prepare_task(repo_path, slug, contract, server_url)
            if prepared.get("phase") == "AWAITING_APPROVAL":
                return self._common(
                    prepared,
                    outcome="INPUT_REQUIRED",
                    summary="The task risk policy requires explicit user approval before dispatch.",
                    next_action="obtain_user_approval",
                )
            with self.coordinator.attach(prepared["task_id"], request_id) as lease:
                kwargs = {"progress": progress} if progress is not None else {}
                return self._service_result(
                    self.bridge.dispatch_and_wait(
                        prepared["task_id"], timeout, lease, **kwargs
                    )
                )

        if name == "reply_and_wait":
            if progress is not None:
                progress("OpenCode received the reply")
            self._keys(
                arguments,
                required={"task_id", "kind", "payload"},
                allowed={"task_id", "kind", "payload", "timeout_seconds"},
                label="arguments",
            )
            task_id = self._task_id(arguments["task_id"])
            kind = self._nonblank(arguments["kind"], "kind")
            payload = self._validate_reply_payload(kind, arguments["payload"])
            timeout = self._timeout(arguments)
            with self.coordinator.attach(task_id, request_id) as lease:
                kwargs = {"progress": progress} if progress is not None else {}
                try:
                    return self._service_result(
                        self.bridge.reply_and_wait(
                            task_id, kind, payload, timeout, lease, **kwargs
                        )
                    )
                except ValueError as error:
                    safe_message = self._reply_input_error(error)
                    if safe_message is None:
                        raise
                    raise ToolInputError(safe_message) from error

        if name == "resume_wait":
            if progress is not None:
                progress("OpenCode wait is resuming")
            self._keys(
                arguments,
                required={"task_id"},
                allowed={"task_id", "timeout_seconds"},
                label="arguments",
            )
            task_id = self._task_id(arguments["task_id"])
            timeout = self._timeout(arguments)
            with self.coordinator.attach(task_id, request_id) as lease:
                kwargs = {"progress": progress} if progress is not None else {}
                return self._service_result(
                    self.bridge.resume_wait(task_id, timeout, lease, **kwargs)
                )

        if name == "task_status":
            self._keys(
                arguments,
                required={"task_id"},
                allowed={"task_id"},
                label="arguments",
            )
            state = self.bridge.status(self._task_id(arguments["task_id"]))
            return self._common(state, artifacts={"state": state})

        if name == "read_transcript":
            self._keys(
                arguments,
                required={"task_id"},
                allowed={"task_id", "cursor", "limit", "include_tool_output"},
                label="arguments",
            )
            task_id = self._task_id(arguments["task_id"])
            cursor = arguments.get("cursor")
            if cursor is not None and (
                not isinstance(cursor, str) or not re.fullmatch(r"0|[1-9][0-9]*", cursor)
            ):
                raise ToolInputError("cursor must be a non-negative decimal string")
            limit = self._integer(arguments.get("limit", 20), "limit", 1, 100)
            include = arguments.get("include_tool_output", False)
            self._boolean(include, "include_tool_output")
            transcript = self.bridge.read_transcript(task_id, cursor, limit, include)
            state = self.bridge.status(task_id)
            return self._common(state, artifacts={"transcript": transcript})

        if name == "collect_result":
            self._keys(
                arguments,
                required={"task_id"},
                allowed={"task_id", "review_evidence"},
                label="arguments",
            )
            task_id = self._task_id(arguments["task_id"])
            evidence = (
                self._review_evidence(arguments["review_evidence"])
                if "review_evidence" in arguments
                else None
            )
            with self.coordinator.attach(task_id, request_id):
                try:
                    collected = self.bridge.collect_result(
                        task_id,
                        review_evidence=evidence,
                    )
                    if evidence is not None:
                        self.bridge.approve_review(task_id, evidence)
                except ValueError as error:
                    message = str(error)
                    if message.startswith("cannot collect"):
                        raise ToolInputError(message) from error
                    raise
            state = self.bridge.status(task_id)
            return self._common(
                state,
                outcome="COMPLETED",
                summary=(
                    "Result collected and Codex review evidence approved."
                    if evidence is not None
                    else "Result collected and ready for Codex review."
                ),
                next_action=("await_integration_decision" if evidence is not None else "review_result"),
                artifacts={"result": collected},
            )

        if name == "cancel_wait":
            self._keys(
                arguments,
                required={"task_id"},
                allowed={"task_id", "reason"},
                label="arguments",
            )
            task_id = self._task_id(arguments["task_id"])
            reason = self._nonblank(arguments.get("reason", "external-cancel"), "reason")
            cancelled = self.coordinator.cancel_task(task_id, reason)
            state = self.bridge.status(task_id)
            result = self._common(
                state,
                outcome="WAIT_CANCELLED" if cancelled else "INTERRUPTED",
                summary=(
                    "The local wait was cancelled; OpenCode continues running."
                    if cancelled
                    else "No active local waiter was found; OpenCode was not aborted."
                ),
                next_action="resume_wait",
            )
            if cancelled:
                result["wait_state"] = "CANCELLED"
            result["cancelled"] = cancelled
            return result

        if name == "abort_task":
            self._keys(
                arguments,
                required={"task_id"},
                allowed={"task_id"},
                label="arguments",
            )
            return self._service_result(
                self.bridge.abort_task(self._task_id(arguments["task_id"]))
            )

        raise AssertionError(f"unhandled tool: {name}")
