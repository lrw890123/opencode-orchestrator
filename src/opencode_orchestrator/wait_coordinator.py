from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import os
import threading

from .cancellation import CancellationToken
from .task_state import TaskLock, TaskLockError, TaskStore, WaitState, utc_now


def _pid_is_alive(pid: int) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass
class WaitLease:
    coordinator: "WaitCoordinator"
    task_id: str
    request_id: str
    token: CancellationToken = field(default_factory=CancellationToken)
    _file_lock: TaskLock | None = field(default=None, init=False, repr=False)
    _attached: bool = field(default=False, init=False, repr=False)

    def __enter__(self) -> "WaitLease":
        self.coordinator._enter(self)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.coordinator._exit(self)
        return False


class WaitCoordinator:
    def __init__(
        self,
        store: TaskStore,
        *,
        pid_is_alive: Callable[[int], bool] = _pid_is_alive,
    ) -> None:
        self.store = store
        self._registry_lock = threading.Lock()
        self._by_task: dict[str, WaitLease] = {}
        self._by_request: dict[str, WaitLease] = {}
        self._pid_is_alive = pid_is_alive
        self._repair_stale_owners()

    def attach(self, task_id: str, request_id: str) -> WaitLease:
        if not request_id or not request_id.strip():
            raise ValueError("request_id must not be blank")
        self.store.load(task_id)
        return WaitLease(self, task_id, request_id)

    def cancel_task(self, task_id: str, reason: str) -> bool:
        with self._registry_lock:
            lease = self._by_task.get(task_id)
        return lease.token.cancel(reason) if lease is not None else False

    def cancel_request(self, request_id: str, reason: str) -> bool:
        with self._registry_lock:
            lease = self._by_request.get(request_id)
        return lease.token.cancel(reason) if lease is not None else False

    def _enter(self, lease: WaitLease) -> None:
        if lease._attached:
            raise RuntimeError("wait lease is already attached")

        file_lock = TaskLock(self.store.task_dir(lease.task_id) / "wait.lock")
        try:
            file_lock.__enter__()
        except TaskLockError as error:
            raise TaskLockError(
                f"waiter is already attached to task: {lease.task_id}"
            ) from error

        try:
            with self._registry_lock:
                if (
                    lease.task_id in self._by_task
                    or lease.request_id in self._by_request
                ):
                    raise TaskLockError(
                        f"waiter is already attached: {lease.task_id}"
                    )
                self._by_task[lease.task_id] = lease
                self._by_request[lease.request_id] = lease

            lease._file_lock = file_lock
            lease._attached = True

            def mark_attached(state: dict) -> None:
                state["wait_state"] = WaitState.ATTACHED.value
                state["wait"] = {
                    "owner_pid": os.getpid(),
                    "request_id": lease.request_id,
                    "attached_at": utc_now(),
                    "disconnect_reason": None,
                }

            self.store.update(lease.task_id, mark_attached)
        except Exception:
            with self._registry_lock:
                if self._by_task.get(lease.task_id) is lease:
                    del self._by_task[lease.task_id]
                if self._by_request.get(lease.request_id) is lease:
                    del self._by_request[lease.request_id]
            lease._file_lock = None
            lease._attached = False
            file_lock.__exit__(None, None, None)
            raise

    def _exit(self, lease: WaitLease) -> None:
        if not lease._attached:
            return

        file_lock = lease._file_lock
        try:
            with self._registry_lock:
                if self._by_task.get(lease.task_id) is lease:
                    del self._by_task[lease.task_id]
                if self._by_request.get(lease.request_id) is lease:
                    del self._by_request[lease.request_id]

            def mark_detached(state: dict) -> None:
                wait = dict(state.get("wait", {}))
                wait["detached_at"] = utc_now()
                wait["disconnect_reason"] = (
                    lease.token.reason if lease.token.cancelled else None
                )
                state["wait"] = wait
                state["wait_state"] = (
                    WaitState.CANCELLED.value
                    if lease.token.cancelled
                    else WaitState.DETACHED.value
                )

            self.store.update(lease.task_id, mark_detached)
        finally:
            lease._attached = False
            lease._file_lock = None
            if file_lock is not None:
                file_lock.__exit__(None, None, None)

    def _repair_stale_owners(self) -> None:
        if not self.store.tasks_root.exists():
            return
        for task_dir in self.store.tasks_root.iterdir():
            if not task_dir.is_dir() or not (task_dir / "state.json").is_file():
                continue
            try:
                state = self.store.load(task_dir.name)
            except (FileNotFoundError, ValueError):
                continue
            if state.get("schema_version") != 3:
                continue
            if state.get("wait_state") != WaitState.ATTACHED.value:
                continue
            owner_pid = state.get("wait", {}).get("owner_pid")
            if self._pid_is_alive(owner_pid):
                continue

            repair_lock = TaskLock(task_dir / "wait.lock")
            try:
                repair_lock.__enter__()
            except TaskLockError:
                continue

            def repair(current: dict) -> None:
                if current.get("wait_state") != WaitState.ATTACHED.value:
                    return
                wait = dict(current.get("wait", {}))
                if self._pid_is_alive(wait.get("owner_pid")):
                    return
                wait["disconnect_reason"] = "stale-owner"
                wait["detached_at"] = utc_now()
                current["wait"] = wait
                current["wait_state"] = WaitState.DETACHED.value

            try:
                self.store.update(task_dir.name, repair)
            finally:
                repair_lock.__exit__(None, None, None)
