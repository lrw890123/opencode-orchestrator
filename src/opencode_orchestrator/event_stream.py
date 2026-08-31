from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
import json
import re
import socket
import time

from .cancellation import CancellationToken


TERMINAL_OR_INPUT_TYPES = {
    "session.idle": "idle",
    "session.error": "error",
    "permission.asked": "permission",
    "permission.v2.asked": "permission",
    "question.asked": "question",
    "question.v2.asked": "question",
}

VISIBLE_PROGRESS_TOOL_NAMES = frozenset(
    {
        "apply_patch",
        "bash",
        "edit",
        "glob",
        "grep",
        "list",
        "read",
        "shell",
        "task",
        "web_fetch",
        "webfetch",
        "write",
    }
)


@dataclass(frozen=True)
class SSEEvent:
    event: str | None
    payload: dict


@dataclass(frozen=True)
class EventOutcome:
    kind: str
    event: dict
    counters: dict[str, int]


def parse_sse_lines(lines: Iterable[bytes]) -> Iterator[SSEEvent]:
    event_name: str | None = None
    data: list[str] = []

    def build() -> SSEEvent | None:
        if not data:
            return None
        return SSEEvent(event_name, json.loads("\n".join(data)))

    for raw in lines:
        line = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        line = line.rstrip("\r\n")
        if not line:
            event = build()
            if event is not None:
                yield event
            event_name = None
            data = []
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "data":
            data.append(value)

    event = build()
    if event is not None:
        yield event


def _session_id(event: dict) -> str | None:
    properties = event.get("properties")
    if not isinstance(properties, dict):
        return None
    return properties.get("sessionID")


def relevant_event(event: dict, session_id: str) -> bool:
    event_type = event.get("type")
    return event_type in TERMINAL_OR_INPUT_TYPES and _session_id(event) == session_id


def _safe_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _safe_error_token(value: object) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        return None
    return value if re.fullmatch(r"[A-Za-z0-9_.:-]+", value) else None


def _safe_error_path(value: object) -> str | None:
    if not isinstance(value, str) or not value.startswith("/") or len(value) > 256:
        return None
    return value if re.fullmatch(r"/[A-Za-z0-9/{}_.:-]*", value) else None


def _safe_string_list(value: object) -> list[str] | None:
    if not isinstance(value, list):
        return None
    if any(not isinstance(item, str) for item in value):
        return None
    return list(value)


def _safe_questions(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    questions: list[dict] = []
    for raw_question in value:
        if not isinstance(raw_question, dict):
            continue
        header = _safe_string(raw_question.get("header"))
        question = _safe_string(raw_question.get("question"))
        if header is None or question is None:
            continue
        visible = {"header": header, "question": question}
        raw_options = raw_question.get("options")
        if isinstance(raw_options, list):
            options = []
            for raw_option in raw_options:
                if not isinstance(raw_option, dict):
                    continue
                label = _safe_string(raw_option.get("label"))
                if label is None:
                    continue
                option = {"label": label}
                description = _safe_string(raw_option.get("description"))
                if description is not None:
                    option["description"] = description
                options.append(option)
            visible["options"] = options
        questions.append(visible)
    return questions


def _sanitized_event(event: dict) -> dict:
    event_type = event.get("type")
    properties = event.get("properties") or {}
    result = {"type": event_type, "properties": {}}
    if not isinstance(properties, dict):
        return result
    visible = result["properties"]
    for key in ("id", "sessionID", "action", "permission"):
        value = _safe_string(properties.get(key))
        if value is not None:
            visible[key] = value
    if event_type in {"permission.asked", "permission.v2.asked"}:
        resources = _safe_string_list(
            properties.get("resources", properties.get("patterns"))
        )
        if resources is not None:
            visible["resources"] = resources
        if isinstance(properties.get("always"), bool):
            visible["always"] = properties["always"]
    elif event_type in {"question.asked", "question.v2.asked"}:
        visible["questions"] = _safe_questions(properties.get("questions"))
    if event_type == "session.error" and isinstance(properties.get("error"), dict):
        error = properties["error"]
        visible_error = {}
        for key in ("name", "code"):
            value = _safe_error_token(error.get(key))
            if value is not None:
                visible_error[key] = value
        status = error.get("status")
        if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
            visible_error["status"] = status
        path = _safe_error_path(error.get("path"))
        if path is not None:
            visible_error["path"] = path
        visible_error["message"] = "OpenCode session failed"
        if visible_error:
            visible["error"] = visible_error
    return result


def sanitize_event_for_log(event: dict) -> dict:
    if event.get("type") in TERMINAL_OR_INPUT_TYPES:
        return _sanitized_event(event)
    properties = event.get("properties") or {}
    logged_properties = {}
    if "sessionID" in properties:
        logged_properties["sessionID"] = properties["sessionID"]
    return {
        "type": event.get("type"),
        "properties": logged_properties,
    }


def progress_message(event: dict) -> str | None:
    """Return a bounded progress summary without copying event bodies."""

    if not isinstance(event, dict):
        return None
    event_type = event.get("type")
    if event_type in {"server.heartbeat", "catalog.updated", "reference.updated"}:
        return None
    if event_type == "server.connected":
        return "OpenCode connection established"
    if event_type in {"message.part.delta", "message.updated"}:
        return "OpenCode is analyzing or editing"
    if event_type == "message.part.updated":
        properties = event.get("properties")
        part = properties.get("part") if isinstance(properties, dict) else None
        if not isinstance(part, dict) or part.get("type") != "tool":
            return "OpenCode is analyzing or editing"
        name = part.get("tool") or part.get("name")
        if name not in VISIBLE_PROGRESS_TOOL_NAMES:
            name = "a tool"
        state = part.get("state")
        status = state.get("status") if isinstance(state, dict) else None
        if status in {"completed", "done", "success"}:
            return f"OpenCode completed {name}"
        if status in {"error", "failed"}:
            return f"OpenCode reported a failure in {name}"
        return f"OpenCode is running {name}"
    if event_type == "session.diff" or (
        isinstance(event_type, str) and event_type.startswith("file.")
    ):
        return "OpenCode updated the worktree"
    if event_type in {"permission.asked", "permission.v2.asked"}:
        return "OpenCode is waiting for permission"
    if event_type in {"question.asked", "question.v2.asked"}:
        return "OpenCode is waiting for an answer"
    if event_type == "session.idle":
        return "OpenCode finished the current execution"
    if event_type == "session.error":
        return "OpenCode execution failed"
    return None


def _interrupt_response(response: object) -> None:
    stream = getattr(response, "fp", None)
    raw = getattr(stream, "raw", None)
    network_socket = getattr(raw, "_sock", None)
    if network_socket is not None:
        try:
            network_socket.shutdown(socket.SHUT_RDWR)
            return
        except OSError:
            pass
    close_response = getattr(response, "close", None)
    if callable(close_response):
        close_response()


def wait_for_session(
    response: Iterable[bytes],
    session_id: str,
    on_connected: Callable[[], None],
    deadline: float,
    cancellation: CancellationToken | None = None,
    on_event: Callable[[dict], None] | None = None,
    on_observed: Callable[[dict, dict[str, int]], EventOutcome | None] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> EventOutcome:
    token = cancellation or CancellationToken()
    event_sink = on_event or (lambda event: None)
    observer = on_observed or (lambda event, counters: None)
    progress_sink = on_progress or (lambda message: None)
    if callable(getattr(response, "close", None)):
        token.add_callback(lambda: _interrupt_response(response))
    counters: Counter[str] = Counter()
    connected = False
    if token.cancelled:
        return EventOutcome("cancelled", {"reason": token.reason}, {})
    for item in parse_sse_lines(response):
        if token.cancelled:
            return EventOutcome("cancelled", {"reason": token.reason}, dict(counters))
        if time.monotonic() > deadline:
            return EventOutcome("timeout", {}, dict(counters))
        event = item.payload
        event_type = str(event.get("type", "unknown"))
        counters[event_type] += 1
        first_connection = event_type == "server.connected" and not connected
        if first_connection:
            on_connected()
            connected = True
            message = progress_message(event)
            if message is not None:
                progress_sink(message)
        elif _session_id(event) == session_id:
            message = progress_message(event)
            if message is not None:
                progress_sink(message)
        sanitized = sanitize_event_for_log(event)
        observed_outcome = observer(sanitized, dict(counters))
        if observed_outcome is not None:
            return observed_outcome
        if first_connection:
            continue
        if _session_id(event) == session_id:
            event_sink(sanitized)
        if relevant_event(event, session_id):
            return EventOutcome(
                TERMINAL_OR_INPUT_TYPES[event_type],
                _sanitized_event(event),
                dict(counters),
            )
    if token.cancelled:
        return EventOutcome("cancelled", {"reason": token.reason}, dict(counters))
    kind = "timeout" if time.monotonic() > deadline else "disconnected"
    return EventOutcome(kind, {}, dict(counters))
