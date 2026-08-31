"""Safe normalization and event projections for OpenCode pending inputs."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Sequence

from .event_stream import EventOutcome


class PendingInputError(ValueError):
    """Raised when a pending OpenCode input cannot be safely normalized."""


@dataclass(frozen=True)
class ReconciliationResult:
    """Result of one bounded pending-input reconciliation pass.

    ``answered`` contains only successful automatic permission replies.  The
    two pending sequences are sanitized projections suitable for task-local
    diagnostics; they never contain OpenCode metadata or request bodies.
    """

    outcome: EventOutcome | None
    answered: Sequence[dict]
    pending_permissions: Sequence[dict]
    pending_questions: Sequence[dict]


def _nonblank_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or not value.strip():
        raise PendingInputError(f"pending input {label} must be a non-blank string")
    if value != value.strip():
        raise PendingInputError(f"pending input {label} must not contain surrounding whitespace")
    return value


def _session_id(raw: dict[str, Any], requested_session_id: str) -> str:
    requested = _nonblank_string(requested_session_id, "session_id")
    actual = _nonblank_string(raw.get("sessionID"), "sessionID")
    if actual != requested:
        raise PendingInputError(
            f"pending input belongs to session {actual}, expected {requested}"
        )
    return actual


def _raw_object(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PendingInputError("pending input must be an object")
    return raw


def _select_field(
    raw: dict[str, Any], modern_key: str, legacy_key: str, label: str
) -> object:
    has_modern = modern_key in raw
    has_legacy = legacy_key in raw
    if not has_modern and not has_legacy:
        raise PendingInputError(f"pending input is missing {label}")
    if has_modern and has_legacy and raw[modern_key] != raw[legacy_key]:
        raise PendingInputError(f"pending input has conflicting {label} fields")
    return raw[modern_key] if has_modern else raw[legacy_key]


def _targets(raw: dict[str, Any]) -> list[str]:
    value = _select_field(raw, "resources", "patterns", "targets")
    if not isinstance(value, list) or not value:
        raise PendingInputError("pending input targets must be a non-empty array")
    if any(not isinstance(target, str) or not target.strip() for target in value):
        raise PendingInputError("pending input targets must contain non-blank strings")
    return deepcopy(value)


def _source_ids(raw: dict[str, Any]) -> tuple[str | None, str | None]:
    sources: list[dict[str, Any]] = []
    for key in ("source", "tool"):
        if key not in raw or raw[key] is None:
            continue
        value = raw[key]
        if not isinstance(value, dict):
            raise PendingInputError(f"pending input {key} must be an object")
        sources.append(value)

    values: dict[str, str | None] = {"messageID": None, "callID": None}
    for source in sources:
        for key in values:
            if key not in source or source[key] is None:
                continue
            value = _nonblank_string(source[key], f"{key}")
            if values[key] is not None and values[key] != value:
                raise PendingInputError(f"pending input has conflicting {key} fields")
            values[key] = value
    return values["messageID"], values["callID"]


def normalize_permission_request(raw: dict, session_id: str) -> dict:
    """Project a legacy or v2 permission request onto the safe policy shape."""

    value = _raw_object(raw)
    request_id = _nonblank_string(value.get("id"), "id")
    actual_session_id = _session_id(value, session_id)
    permission = _nonblank_string(
        _select_field(value, "action", "permission", "permission"), "permission"
    )
    patterns = _targets(value)
    message_id, call_id = _source_ids(value)
    return {
        "request_id": request_id,
        "session_id": actual_session_id,
        "permission": permission,
        "patterns": patterns,
        "metadata": {},
        "message_id": message_id,
        "call_id": call_id,
    }


def _question_text(value: object, label: str) -> str:
    return _nonblank_string(value, f"question.{label}")


def _question_list(raw: dict[str, Any]) -> list[dict[str, Any]]:
    value = raw.get("questions")
    if not isinstance(value, list) or not value:
        raise PendingInputError("pending input questions must be a non-empty array")

    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise PendingInputError(f"pending input questions[{index}] must be an object")
        question = {
            "header": _question_text(item.get("header"), "header"),
            "question": _question_text(item.get("question"), "question"),
        }
        if "options" in item:
            options = item["options"]
            if not isinstance(options, list):
                raise PendingInputError(
                    f"pending input questions[{index}].options must be an array"
                )
            normalized_options: list[dict[str, str]] = []
            for option_index, option in enumerate(options):
                if not isinstance(option, dict):
                    raise PendingInputError(
                        "pending input "
                        f"questions[{index}].options[{option_index}] must be an object"
                    )
                if "label" not in option:
                    raise PendingInputError(
                        f"pending input questions[{index}].options[{option_index}] is missing label"
                    )
                normalized_option = {"label": _nonblank_string(option["label"], "option.label")}
                if "description" in option:
                    description = option["description"]
                    if not isinstance(description, str):
                        raise PendingInputError(
                            "pending input "
                            f"questions[{index}].options[{option_index}].description "
                            "must be a string"
                        )
                    normalized_option["description"] = description
                normalized_options.append(normalized_option)
            question["options"] = normalized_options
        normalized.append(question)
    return normalized


def normalize_question_request(raw: dict, session_id: str) -> dict:
    """Project a question request onto request/session IDs and visible choices."""

    value = _raw_object(raw)
    request_id = _nonblank_string(value.get("id"), "id")
    actual_session_id = _session_id(value, session_id)
    return {
        "request_id": request_id,
        "session_id": actual_session_id,
        "questions": _question_list(value),
    }


def permission_event(request: dict) -> dict:
    """Create a sanitized event projection from a normalized permission."""

    properties: dict[str, Any] = {
        "id": request["request_id"],
        "sessionID": request["session_id"],
        "action": request["permission"],
        "resources": deepcopy(request["patterns"]),
    }
    return {"type": "permission.reconciled", "properties": properties}


def question_event(request: dict) -> dict:
    """Create a sanitized event projection from a normalized question."""

    questions = []
    for item in request["questions"]:
        visible = {
            "header": item["header"],
            "question": item["question"],
        }
        if "options" in item:
            visible["options"] = [
                {
                    key: option[key]
                    for key in ("label", "description")
                    if key in option
                }
                for option in item["options"]
            ]
        questions.append(visible)
    return {
        "type": "question.reconciled",
        "properties": {
            "id": request["request_id"],
            "sessionID": request["session_id"],
            "questions": questions,
        },
    }
