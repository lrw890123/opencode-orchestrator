from __future__ import annotations


TASK_SCHEMA_VERSION = 3

TIMEOUT_MIN_SECONDS = 1
TIMEOUT_MAX_SECONDS = 86400
DEFAULT_TIMEOUT_SECONDS = 3600

DEFAULT_PERMISSION_ACTION = "allow"
DEFAULT_PERMISSION_PERSISTENCE = "task"

INPUT_PROBE_MIN_SECONDS = 5
INPUT_PROBE_MAX_SECONDS = 300
DEFAULT_INPUT_PROBE_SECONDS = 15
STALL_TIMEOUT_MIN_SECONDS = 30
STALL_TIMEOUT_MAX_SECONDS = 86400
DEFAULT_STALL_TIMEOUT_SECONDS = 600

TASK_CONTRACT_FIELDS = (
    "goal",
    "non_goals",
    "approved_plan",
    "allowed_paths",
    "forbidden_actions",
    "acceptance_criteria",
    "test_commands",
    "risk",
    "user_approved",
)
TASK_CONTRACT_KEYS = frozenset(TASK_CONTRACT_FIELDS)

RISK_FIELDS = (
    "file_count",
    "line_count",
    "cross_module",
    "public_interface",
    "dependency_change",
    "high_risk_actions",
)
RISK_KEYS = frozenset(RISK_FIELDS)


def default_permission_policy() -> dict:
    return {
        "default": DEFAULT_PERMISSION_ACTION,
        "persistence": DEFAULT_PERMISSION_PERSISTENCE,
        "approval_basis": None,
        "rules": [],
    }


def default_progress_policy() -> dict:
    return {
        "input_probe_interval_seconds": DEFAULT_INPUT_PROBE_SECONDS,
        "stall_timeout_seconds": DEFAULT_STALL_TIMEOUT_SECONDS,
    }


def default_progress_state(timestamp: str, event: str) -> dict:
    return {
        "last_progress_at": timestamp,
        "last_progress_event": event,
        "idle_seconds": 0,
        "heartbeat_count": 0,
        "pending_tools": [],
        "pending_permissions": [],
        "pending_questions": [],
        "diagnostic_error": None,
        "last_input_probe_at": None,
    }
