from __future__ import annotations

from pathlib import Path

from .git_workspace import GitWorkspace
from .policy import validate_allowed_paths
from .task_state import atomic_write_json


PRIVATE_PART_TYPES = {"reasoning", "thinking", "analysis"}


def last_assistant_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if (message.get("info") or {}).get("role") != "assistant":
            continue
        parts = message.get("parts") or []
        texts = [
            str(part.get("text"))
            for part in parts
            if part.get("type") == "text" and part.get("text")
        ]
        if texts:
            return "\n".join(texts)
    return ""


def truncate_text(text: str, limit: int = 32768) -> tuple[str, bool]:
    if limit < 0:
        raise ValueError("limit must be non-negative")
    return (text[:limit], len(text) > limit)


def _tool_part(part: dict, include_tool_output: bool) -> dict:
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    normalized = {
        "type": "tool",
        "name": part.get("tool") or part.get("name"),
        "status": state.get("status") or part.get("status"),
    }
    if include_tool_output:
        output = state.get("output", part.get("output", part.get("result")))
        if output is not None:
            normalized["output"] = output
    return {key: value for key, value in normalized.items() if value is not None}


def normalize_messages(
    messages: list[dict],
    *,
    include_tool_output: bool = False,
) -> list[dict]:
    normalized_messages = []
    for index, message in enumerate(messages):
        info = message.get("info") if isinstance(message.get("info"), dict) else {}
        timestamp = info.get("created_at") or info.get("createdAt")
        if timestamp is None and isinstance(info.get("time"), dict):
            timestamp = info["time"].get("created")
        parts = []
        for part in message.get("parts") or []:
            if not isinstance(part, dict):
                continue
            part_type = str(part.get("type", "unknown"))
            if part_type in PRIVATE_PART_TYPES or "reasoning" in part_type:
                continue
            if part_type == "text":
                if part.get("text") is not None:
                    parts.append({"type": "text", "text": str(part["text"])})
                continue
            if part_type.startswith("tool") or part_type == "tool":
                parts.append(_tool_part(part, include_tool_output))
                continue
            compact = {"type": part_type}
            for key in ("name", "status"):
                if part.get(key) is not None:
                    compact[key] = part[key]
            parts.append(compact)
        normalized_messages.append(
            {
                "index": index,
                "role": info.get("role") or message.get("role") or "unknown",
                "created_at": timestamp,
                "parts": parts,
            }
        )
    return normalized_messages


def collect_git_evidence(
    worktree: Path,
    base_sha: str,
    allowed_paths: list[str],
) -> dict:
    workspace = GitWorkspace(worktree)
    changed = workspace.changed_files(base_sha)
    untracked = workspace.untracked_files()
    return {
        "changed_files": changed,
        "untracked_files": untracked,
        "diff_stat": workspace.diff_stat(base_sha),
        "out_of_scope": validate_allowed_paths(changed + untracked, allowed_paths),
    }


def write_result(task_dir: Path, result: dict) -> Path:
    destination = Path(task_dir) / "result.json"
    atomic_write_json(destination, result)
    return destination
