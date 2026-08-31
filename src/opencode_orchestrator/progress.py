"""Safe projections used for OpenCode progress and stall diagnostics.

The functions in this module intentionally operate on event/message metadata
only.  They never copy message text, tool input, tool output, or arbitrary
metadata into a diagnostic projection.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
from typing import Any


TRANSPORT_EVENT_TYPES = frozenset({"server.connected", "server.heartbeat"})

# Keep this table explicit.  A target-session event is not automatically task
# progress merely because it has a session ID: transport and unrelated server
# events must not move the progress clock.
MEANINGFUL_EVENT_TYPES = frozenset(
    {
        "message.updated",
        "session.diff",
        "session.idle",
        "session.error",
    }
)
MEANINGFUL_EVENT_PREFIXES = (
    "message.part.",
    "permission.",
    "question.",
    "tool.",
    "file.",
    "session.diff.",
)

PENDING_TOOL_STATUSES = frozenset(
    {"pending", "running", "started", "in_progress", "in-progress"}
)
MAX_FUTURE_CLOCK_SKEW_SECONDS = 300


def _properties(event: object) -> dict[str, Any] | None:
    if not isinstance(event, dict):
        return None
    properties = event.get("properties")
    return properties if isinstance(properties, dict) else None


def _event_type(event: object) -> str | None:
    if not isinstance(event, dict):
        return None
    value = event.get("type")
    return value if isinstance(value, str) and value.strip() else None


def is_meaningful_progress(event: dict, session_id: str) -> bool:
    """Return whether ``event`` advances meaningful work for ``session_id``.

    Only explicitly known event types (or their narrowly scoped prefixes) are
    considered.  Connected/heartbeat events are always transport-only.
    """

    event_type = _event_type(event)
    if event_type is None or event_type in TRANSPORT_EVENT_TYPES:
        return False
    if not isinstance(session_id, str) or not session_id.strip():
        return False
    properties = _properties(event)
    if properties is None or properties.get("sessionID") != session_id:
        return False
    return event_type in MEANINGFUL_EVENT_TYPES or event_type.startswith(
        MEANINGFUL_EVENT_PREFIXES
    )


def _nonblank(value: object) -> str | None:
    if not isinstance(value, str) or not value or not value.strip():
        return None
    if value != value.strip():
        return None
    return value


def _parse_timestamp(value: object) -> tuple[str, datetime] | None:
    """Parse an ISO timestamp or OpenCode epoch-millisecond timestamp."""

    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            finite = math.isfinite(value)
        except (OverflowError, ValueError):
            return None
        if not finite:
            return None
        try:
            parsed = datetime.fromtimestamp(value / 1000, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
        return parsed.isoformat(), parsed
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    # Keep a caller-provided ISO representation for readable diagnostics while
    # using the normalized aware value for ordering.
    return value, parsed


def _tool_timestamp(part: dict[str, Any], state: dict[str, Any]) -> tuple[str, datetime] | None:
    tool_time = state.get("time")
    if not isinstance(tool_time, dict):
        return None
    return _parse_timestamp(tool_time.get("start"))


def _tool_part(part: object) -> tuple[dict[str, str], datetime] | None:
    if not isinstance(part, dict):
        return None
    part_type = part.get("type")
    if part_type != "tool":
        return None
    state = part.get("state")
    if not isinstance(state, dict):
        return None
    status = state.get("status")
    if not isinstance(status, str) or status not in PENDING_TOOL_STATUSES:
        return None
    name = _nonblank(part.get("tool") or part.get("name"))
    part_id = _nonblank(part.get("id") or part.get("partID") or part.get("partId"))
    call_id = _nonblank(part.get("callID") or part.get("callId"))
    started = _tool_timestamp(part, state)
    if name is None or part_id is None or call_id is None or started is None:
        return None
    started_at, parsed = started
    return (
        {
            "name": name,
            "part_id": part_id,
            "call_id": call_id,
            "status": status,
            "started_at": started_at,
        },
        parsed,
    )


def pending_tools(messages: list[dict]) -> list[dict]:
    """Project currently running tool parts without exposing their bodies."""

    if not isinstance(messages, list):
        return []
    unique: dict[tuple[str, str], tuple[dict[str, str], datetime]] = {}
    for message in messages:
        if not isinstance(message, dict):
            continue
        parts = message.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            projected = _tool_part(part)
            if projected is None:
                continue
            compact, parsed = projected
            unique[(compact["part_id"], compact["call_id"])] = (compact, parsed)

    ordered = sorted(
        unique.values(),
        key=lambda item: (item[1], item[0]["part_id"]),
    )
    return [compact for compact, _ in ordered]


def _message_timestamps(message: object) -> list[tuple[str, datetime]]:
    if not isinstance(message, dict):
        return []
    info = message.get("info")
    if not isinstance(info, dict):
        info = message
    candidates: list[tuple[str, datetime]] = []
    for key in (
        "created_at",
        "createdAt",
        "updated_at",
        "updatedAt",
        "completed_at",
        "completedAt",
    ):
        if key in info:
            parsed = _parse_timestamp(info[key])
            if parsed is not None:
                candidates.append(parsed)
    timestamp = info.get("time")
    if isinstance(timestamp, dict):
        for key in ("created", "updated", "completed"):
            if key in timestamp:
                parsed = _parse_timestamp(timestamp[key])
                if parsed is not None:
                    candidates.append(parsed)

    parts = message.get("parts")
    if isinstance(parts, list):
        for part in parts:
            if not isinstance(part, dict) or part.get("type") != "tool":
                continue
            state = part.get("state")
            if not isinstance(state, dict):
                continue
            tool_time = state.get("time")
            if not isinstance(tool_time, dict):
                continue
            for key in ("start", "end"):
                if key in tool_time:
                    parsed = _parse_timestamp(tool_time[key])
                    if parsed is not None:
                        candidates.append(parsed)
    return candidates


def latest_message_progress_at(
    messages: list[dict],
    *,
    observed_at: str | None = None,
) -> str | None:
    """Return the newest safe message or tool-state progress timestamp."""

    if not isinstance(messages, list):
        return None
    if observed_at is None:
        observation = datetime.now(timezone.utc)
    else:
        parsed_observation = _parse_timestamp(observed_at)
        if parsed_observation is None:
            raise ValueError("observed_at must be a valid timestamp")
        observation = parsed_observation[1]
    latest_allowed = observation + timedelta(
        seconds=MAX_FUTURE_CLOCK_SKEW_SECONDS
    )
    latest: tuple[datetime, int, str] | None = None
    for index, message in enumerate(messages):
        for timestamp, instant in _message_timestamps(message):
            if instant > latest_allowed:
                continue
            candidate = (instant, index, timestamp)
            if latest is None or candidate[:2] >= latest[:2]:
                latest = candidate
    return latest[2] if latest is not None else None


def _parse_iso(value: str) -> datetime:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("timestamp must be a non-blank ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("timestamp must be a valid ISO-8601 string") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def idle_seconds(last_progress_at: str, now: str) -> int:
    """Return elapsed whole seconds, clamped to zero for a future baseline."""

    elapsed = (_parse_iso(now) - _parse_iso(last_progress_at)).total_seconds()
    return max(0, int(elapsed))
