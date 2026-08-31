from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Callable

from .contracts import TASK_SCHEMA_VERSION
from .collector import (
    collect_git_evidence,
    last_assistant_text,
    normalize_messages,
    truncate_text,
    write_result,
)
from .git_workspace import GitWorkspace
from .task_state import (
    ExecutionState,
    Phase,
    ReviewState,
    TaskStore,
    atomic_write_json,
    utc_now,
)


class ResultService:
    """Own transcript projection, result collection, and review state changes."""

    def __init__(self, store: TaskStore, client_for_state: Callable[[dict], object]):
        self.store = store
        self.client_for_state = client_for_state

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
        messages = self.client_for_state(state).messages(session_id, limit=10000)
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
            "schema_version": TASK_SCHEMA_VERSION,
            "task_id": task_id,
            "session_id": session_id,
            "messages": page,
            "next_cursor": str(end) if end < len(normalized) else None,
        }

    def collect_result(self, task_id: str, review_evidence: dict | None = None) -> dict:
        state = self.store.load(task_id)
        if state["execution_state"] != ExecutionState.COMPLETED.value:
            raise ValueError(
                f"cannot collect task in execution state {state['execution_state']}"
            )
        client = self.client_for_state(state)
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
            "schema_version": TASK_SCHEMA_VERSION,
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
