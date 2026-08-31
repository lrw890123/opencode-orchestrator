from __future__ import annotations

from copy import deepcopy

from .contracts import (
    DEFAULT_INPUT_PROBE_SECONDS,
    DEFAULT_PERMISSION_ACTION,
    DEFAULT_PERMISSION_PERSISTENCE,
    DEFAULT_STALL_TIMEOUT_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    INPUT_PROBE_MAX_SECONDS,
    INPUT_PROBE_MIN_SECONDS,
    RISK_FIELDS,
    STALL_TIMEOUT_MAX_SECONDS,
    STALL_TIMEOUT_MIN_SECONDS,
    TASK_CONTRACT_FIELDS,
    TASK_SCHEMA_VERSION,
    TIMEOUT_MAX_SECONDS,
    TIMEOUT_MIN_SECONDS,
)


PERMISSION_POLICY_SCHEMA = {
    "type": "object",
    "properties": {
        "default": {
            "type": "string",
            "enum": ["allow", "ask", "deny"],
            "default": DEFAULT_PERMISSION_ACTION,
        },
        "persistence": {
            "type": "string",
            "enum": ["task", "project"],
            "default": DEFAULT_PERMISSION_PERSISTENCE,
        },
        "approval_basis": {
            "anyOf": [
                {"type": "string", "minLength": 1},
                {"type": "null"},
            ]
        },
        "rules": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "permission": {"type": "string", "minLength": 1},
                    "pattern": {"type": "string", "minLength": 1},
                    "action": {"type": "string", "enum": ["allow", "ask", "deny"]},
                },
                "required": ["permission", "pattern", "action"],
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}

PROGRESS_POLICY_SCHEMA = {
    "type": "object",
    "properties": {
        "input_probe_interval_seconds": {
            "type": "integer",
            "minimum": INPUT_PROBE_MIN_SECONDS,
            "maximum": INPUT_PROBE_MAX_SECONDS,
            "default": DEFAULT_INPUT_PROBE_SECONDS,
        },
        "stall_timeout_seconds": {
            "type": "integer",
            "minimum": STALL_TIMEOUT_MIN_SECONDS,
            "maximum": STALL_TIMEOUT_MAX_SECONDS,
            "default": DEFAULT_STALL_TIMEOUT_SECONDS,
        },
    },
    "additionalProperties": False,
}


TASK_ID_SCHEMA = {
    "type": "string",
    "minLength": 1,
    "pattern": "^oc-[A-Za-z0-9._-]+$",
}

MODEL_SCHEMA = {
    "type": "object",
    "properties": {
        "providerID": {"type": "string", "minLength": 1},
        "modelID": {"type": "string", "minLength": 1},
    },
    "required": ["providerID", "modelID"],
    "additionalProperties": False,
}

RISK_SCHEMA = {
    "type": "object",
    "properties": {
        "file_count": {"type": "integer", "minimum": 0},
        "line_count": {"type": "integer", "minimum": 0},
        "cross_module": {"type": "boolean"},
        "public_interface": {"type": "boolean"},
        "dependency_change": {"type": "boolean"},
        "high_risk_actions": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": list(RISK_FIELDS),
    "additionalProperties": False,
}

TASK_CONTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "goal": {"type": "string", "minLength": 1},
        "non_goals": {"type": "array", "items": {"type": "string"}},
        "approved_plan": {"type": "array", "items": {"type": "string"}},
        "allowed_paths": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "minLength": 1},
        },
        "forbidden_actions": {"type": "array", "items": {"type": "string"}},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "test_commands": {"type": "array", "items": {"type": "string"}},
        "risk": RISK_SCHEMA,
        "user_approved": {"type": "boolean"},
    },
    "required": list(TASK_CONTRACT_FIELDS),
    "additionalProperties": False,
}

REVIEW_EVIDENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "tests_passed": {"const": True},
        "review_summary": {"type": "string", "minLength": 1},
    },
    "required": ["tests_passed", "review_summary"],
    "additionalProperties": False,
}

COMMON_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "schema_version": {"const": TASK_SCHEMA_VERSION},
        "task_id": TASK_ID_SCHEMA,
        "outcome": {
            "type": "string",
            "enum": [
                "COMPLETED",
                "INPUT_REQUIRED",
                "FAILED",
                "WAIT_CANCELLED",
                "ABORTED",
                "INTERRUPTED",
                "STALLED",
            ],
        },
        "execution_state": {
            "type": "string",
            "enum": [
                "PREPARING",
                "RUNNING",
                "INPUT_REQUIRED",
                "COMPLETED",
                "FAILED",
                "ABORTED",
                "STALLED",
            ],
        },
        "wait_state": {
            "type": "string",
            "enum": ["DETACHED", "ATTACHED", "CANCELLED"],
        },
        "summary": {"type": "string", "minLength": 1},
        "next_action": {"type": "string", "minLength": 1},
        "artifacts": {"type": "object"},
    },
    "required": [
        "schema_version",
        "task_id",
        "outcome",
        "execution_state",
        "wait_state",
        "summary",
        "next_action",
        "artifacts",
    ],
    "additionalProperties": True,
}


def _definition(
    name: str,
    description: str,
    input_schema: dict,
    *,
    read_only: bool = False,
    destructive: bool = False,
) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": input_schema,
        "outputSchema": deepcopy(COMMON_OUTPUT_SCHEMA),
        "annotations": {
            "readOnlyHint": read_only,
            "destructiveHint": destructive,
        },
    }


TOOL_DEFINITIONS = (
    _definition(
        "delegate_and_wait",
        "Create an isolated OpenCode task, dispatch the approved contract, and wait without model polling.",
        {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string", "minLength": 1},
                "task_contract": TASK_CONTRACT_SCHEMA,
                "slug": {"type": "string", "minLength": 1},
                "model": MODEL_SCHEMA,
                "effort": {"type": "string", "minLength": 1, "default": "max"},
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": TIMEOUT_MIN_SECONDS,
                    "maximum": TIMEOUT_MAX_SECONDS,
                    "default": DEFAULT_TIMEOUT_SECONDS,
                },
                "server_url": {"type": "string", "minLength": 1},
                "permission_policy": deepcopy(PERMISSION_POLICY_SCHEMA),
                "progress_policy": deepcopy(PROGRESS_POLICY_SCHEMA),
            },
            "required": ["repo_path", "task_contract"],
            "additionalProperties": False,
        },
    ),
    _definition(
        "reply_and_wait",
        (
            "Reply to an OpenCode question, permission request, review round, or explicitly "
            "continue an idle paused session, then keep waiting. Approving sensitive "
            "permissions requires user_approved=true and an action-specific approval_basis "
            "naming the permission and target."
        ),
        {
            "type": "object",
            "properties": {
                "task_id": TASK_ID_SCHEMA,
                "kind": {
                    "type": "string",
                    "enum": ["review", "continue", "permission", "question"],
                },
                "payload": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "minLength": 1},
                        "request_id": {"type": "string", "minLength": 1},
                        "response": {"type": "string", "enum": ["once", "always", "reject"]},
                        "answers": {
                            "type": "array",
                            "items": {"type": "array", "items": {"type": "string"}},
                        },
                        "user_approved": {
                            "type": "boolean",
                            "description": "Must be true to approve a sensitive permission.",
                        },
                        "approval_basis": {
                            "type": "string",
                            "minLength": 1,
                            "description": (
                                "For sensitive permission approval, name the exact permission "
                                "and target pattern approved by the user."
                            ),
                        },
                        "remember_for_task": {
                            "type": "boolean",
                            "description": (
                                "With response=once and explicit approval, remember the exact "
                                "live permission patterns only for this orchestrator task."
                            ),
                        },
                    },
                    "additionalProperties": False,
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": TIMEOUT_MIN_SECONDS,
                    "maximum": TIMEOUT_MAX_SECONDS,
                    "default": DEFAULT_TIMEOUT_SECONDS,
                },
            },
            "required": ["task_id", "kind", "payload"],
            "additionalProperties": False,
        },
    ),
    _definition(
        "resume_wait",
        "Resume a detached wait for an existing OpenCode task without resending completed dispatches.",
        {
            "type": "object",
            "properties": {
                "task_id": TASK_ID_SCHEMA,
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": TIMEOUT_MIN_SECONDS,
                    "maximum": TIMEOUT_MAX_SECONDS,
                    "default": DEFAULT_TIMEOUT_SECONDS,
                },
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    ),
    _definition(
        "task_status",
        "Read one persisted orchestration status snapshot; do not use this tool for polling.",
        {
            "type": "object",
            "properties": {"task_id": TASK_ID_SCHEMA},
            "required": ["task_id"],
            "additionalProperties": False,
        },
        read_only=True,
    ),
    _definition(
        "read_transcript",
        "Read a safe, paginated OpenCode transcript; tool output bodies are opt-in.",
        {
            "type": "object",
            "properties": {
                "task_id": TASK_ID_SCHEMA,
                "cursor": {"type": "string", "pattern": "^(0|[1-9][0-9]*)$"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                "include_tool_output": {"type": "boolean", "default": False},
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
        read_only=True,
    ),
    _definition(
        "collect_result",
        "Collect OpenCode messages and Git evidence without merging, pushing, or cleaning up.",
        {
            "type": "object",
            "properties": {
                "task_id": TASK_ID_SCHEMA,
                "review_evidence": REVIEW_EVIDENCE_SCHEMA,
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    ),
    _definition(
        "cancel_wait",
        "Cancel only the local pending wait; OpenCode execution continues.",
        {
            "type": "object",
            "properties": {
                "task_id": TASK_ID_SCHEMA,
                "reason": {"type": "string", "minLength": 1},
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    ),
    _definition(
        "abort_task",
        "Explicitly abort the OpenCode session while preserving its task record and worktree.",
        {
            "type": "object",
            "properties": {"task_id": TASK_ID_SCHEMA},
            "required": ["task_id"],
            "additionalProperties": False,
        },
        destructive=True,
    ),
)
