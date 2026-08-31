from __future__ import annotations

from copy import deepcopy
from enum import Enum
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import secrets
import time
from typing import Any, Callable


class Phase:
    DRAFT = "DRAFT"
    RISK_CHECK = "RISK_CHECK"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    PREPARING = "PREPARING"
    DISPATCHED = "DISPATCHED"
    RUNNING = "RUNNING"
    NEEDS_INPUT = "NEEDS_INPUT"
    PERMISSION_WAIT = "PERMISSION_WAIT"
    PAUSED = "PAUSED"
    STALLED = "STALLED"
    FAILED = "FAILED"
    COLLECTING = "COLLECTING"
    REVIEWING = "REVIEWING"
    REVISION_REQUESTED = "REVISION_REQUESTED"
    PASSED = "PASSED"
    AWAITING_INTEGRATION = "AWAITING_INTEGRATION"
    CANCELLED = "CANCELLED"


class ExecutionState(str, Enum):
    PREPARING = "PREPARING"
    RUNNING = "RUNNING"
    INPUT_REQUIRED = "INPUT_REQUIRED"
    STALLED = "STALLED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


class WaitState(str, Enum):
    DETACHED = "DETACHED"
    ATTACHED = "ATTACHED"
    CANCELLED = "CANCELLED"


class ReviewState(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    REVIEWING = "REVIEWING"
    REVISION_REQUESTED = "REVISION_REQUESTED"
    PASSED = "PASSED"
    AWAITING_INTEGRATION = "AWAITING_INTEGRATION"


TRANSITIONS = {
    Phase.DRAFT: {Phase.RISK_CHECK},
    Phase.RISK_CHECK: {Phase.AWAITING_APPROVAL, Phase.PREPARING, Phase.FAILED},
    Phase.AWAITING_APPROVAL: {Phase.PREPARING, Phase.CANCELLED},
    Phase.PREPARING: {Phase.DISPATCHED, Phase.FAILED, Phase.CANCELLED},
    Phase.DISPATCHED: {Phase.RUNNING, Phase.PAUSED, Phase.FAILED, Phase.CANCELLED},
    Phase.RUNNING: {
        Phase.NEEDS_INPUT,
        Phase.PERMISSION_WAIT,
        Phase.PAUSED,
        Phase.STALLED,
        Phase.FAILED,
        Phase.COLLECTING,
        Phase.CANCELLED,
    },
    Phase.NEEDS_INPUT: {Phase.RUNNING, Phase.FAILED, Phase.CANCELLED},
    Phase.PERMISSION_WAIT: {Phase.RUNNING, Phase.FAILED, Phase.CANCELLED},
    Phase.PAUSED: {Phase.RUNNING, Phase.STALLED, Phase.FAILED, Phase.CANCELLED},
    Phase.STALLED: {
        Phase.RUNNING,
        Phase.NEEDS_INPUT,
        Phase.PERMISSION_WAIT,
        Phase.FAILED,
        Phase.COLLECTING,
        Phase.CANCELLED,
    },
    Phase.COLLECTING: {Phase.REVIEWING, Phase.FAILED},
    Phase.REVIEWING: {Phase.REVISION_REQUESTED, Phase.PASSED, Phase.FAILED},
    Phase.REVISION_REQUESTED: {Phase.RUNNING, Phase.FAILED, Phase.CANCELLED},
    Phase.PASSED: {Phase.AWAITING_INTEGRATION},
    Phase.AWAITING_INTEGRATION: set(),
    Phase.FAILED: set(),
    Phase.CANCELLED: set(),
}


class TaskLockError(RuntimeError):
    """Raised when another process or file descriptor owns a task lock."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_task_id(now: str | None = None, entropy: str | None = None) -> str:
    timestamp = now or datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = entropy or secrets.token_hex(4)
    return f"oc-{timestamp}-{suffix}"


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class TaskLock:
    def __init__(self, path: Path):
        self.path = path
        self._handle = None

    def __enter__(self) -> "TaskLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self._handle.close()
            self._handle = None
            raise TaskLockError(f"task lock is already held: {self.path}") from error
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._handle is None:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None


class TaskStore:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self.tasks_root = self.root / "tasks"

    def task_dir(self, task_id: str) -> Path:
        if not task_id.startswith("oc-") or "/" in task_id or ".." in task_id:
            raise ValueError(f"invalid task id: {task_id}")
        return self.tasks_root / task_id

    def create(
        self,
        task_id: str,
        repo_root: str,
        base_sha: str,
        original_branch: str,
        dirty_fingerprint: str,
    ) -> dict[str, Any]:
        directory = self.task_dir(task_id)
        state_path = directory / "state.json"
        if state_path.exists():
            raise FileExistsError(f"task already exists: {task_id}")
        now = utc_now()
        state = {
            "schema_version": 3,
            "task_id": task_id,
            "task_fingerprint": None,
            "phase": Phase.DRAFT,
            "execution_state": ExecutionState.PREPARING.value,
            "wait_state": WaitState.DETACHED.value,
            "review_state": ReviewState.PENDING.value,
            "created_at": now,
            "updated_at": now,
            "source": {
                "repo_root": repo_root,
                "base_sha": base_sha,
                "original_branch": original_branch,
                "dirty_fingerprint": dirty_fingerprint,
            },
            "worktree": {},
            "opencode": {},
            "policy": {},
            "permission_policy": {
                "default": "allow",
                "persistence": "task",
                "approval_basis": None,
                "rules": [],
            },
            "progress_policy": {
                "input_probe_interval_seconds": 15,
                "stall_timeout_seconds": 600,
            },
            "permission_audit": [],
            "progress": {
                "last_progress_at": now,
                "last_progress_event": "task.created",
                "idle_seconds": 0,
                "heartbeat_count": 0,
                "pending_tools": [],
                "pending_permissions": [],
                "pending_questions": [],
                "diagnostic_error": None,
                "last_input_probe_at": None,
            },
            "execution": {
                "dispatch_marker": f"[oc-task:{task_id}]",
                "sse_reconnects": 0,
                "poll_fallback_used": False,
                "review_round": 0,
                "continuation_round": 0,
                "continuation": None,
            },
        }
        atomic_write_json(state_path, state)
        return state

    def load(self, task_id: str) -> dict[str, Any]:
        with (self.task_dir(task_id) / "state.json").open(encoding="utf-8") as handle:
            return json.load(handle)

    def save(self, task_id: str, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("task_id") != task_id:
            raise ValueError("state task_id does not match destination")
        saved = dict(state)
        saved["updated_at"] = utc_now()
        atomic_write_json(self.task_dir(task_id) / "state.json", saved)
        return saved

    def transition(self, task_id: str, phase: str, **updates: Any) -> dict[str, Any]:
        state = self.load(task_id)
        current = state["phase"]
        if phase not in TRANSITIONS.get(current, set()):
            raise ValueError(f"invalid phase transition: {current} -> {phase}")
        state["phase"] = phase
        state.update(updates)
        return self.save(task_id, state)

    def update(
        self,
        task_id: str,
        mutator: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        for attempt in range(51):
            lock = self.lock(task_id)
            try:
                lock.__enter__()
            except TaskLockError:
                if attempt == 50:
                    raise
                time.sleep(0.01)
                continue
            try:
                state = deepcopy(self.load(task_id))
                mutator(state)
                return self.save(task_id, state)
            finally:
                lock.__exit__(None, None, None)
        raise AssertionError("unreachable task update retry state")

    def lock(self, task_id: str) -> TaskLock:
        return TaskLock(self.task_dir(task_id) / "task.lock")
