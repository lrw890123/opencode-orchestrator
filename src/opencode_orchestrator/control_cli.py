from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import socket
from typing import Callable

from .control_server import control_socket_path, control_token_path
from .migration import resolve_state_roots
from .service import BridgeService
from .task_state import TaskStore, WaitState, utc_now
from .wait_coordinator import _pid_is_alive


ALLOWED_ACTIONS = {"status", "cancel-wait", "abort-task"}
MAX_RESPONSE_BYTES = 64 * 1024


class ControlClient:
    def __init__(
        self,
        state_root: Path,
        *,
        pid_is_alive: Callable[[int], bool] = _pid_is_alive,
    ) -> None:
        self.state_root = Path(state_root).expanduser().resolve()
        self.control_dir = self.state_root / "control"
        self.socket_path = control_socket_path(self.state_root)
        self.token_path = control_token_path(self.state_root)
        self.store = TaskStore(self.state_root)
        self._pid_is_alive = pid_is_alive

    @staticmethod
    def _owner_pid(state: dict) -> int | None:
        owner_pid = (state.get("wait") or {}).get("owner_pid")
        if isinstance(owner_pid, int) and not isinstance(owner_pid, bool) and owner_pid > 0:
            return owner_pid
        return None

    def _online_endpoint(self, task_id: str) -> tuple[Path, Path, int | None]:
        state = self.store.load(task_id)
        owner_pid = self._owner_pid(state)
        if owner_pid is not None:
            socket_path = control_socket_path(self.state_root, owner_pid)
            token_path = control_token_path(self.state_root, owner_pid)
            if socket_path.exists() and token_path.is_file():
                return socket_path, token_path, owner_pid
        return self.socket_path, self.token_path, owner_pid

    def _online_request(
        self,
        action: str,
        task_id: str,
        endpoint: tuple[Path, Path, int | None] | None = None,
    ) -> dict:
        socket_path, token_path, _ = endpoint or self._online_endpoint(task_id)
        token = token_path.read_text(encoding="utf-8")
        payload = {
            "action": action,
            "task_id": task_id,
            "nonce": secrets.token_urlsafe(18),
            "token": token,
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        response = bytearray()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3)
            connection.connect(str(socket_path))
            connection.sendall(encoded)
            while b"\n" not in response:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
                if len(response) > MAX_RESPONSE_BYTES:
                    raise RuntimeError("control response exceeds 64 KiB")
        line, separator, remainder = bytes(response).partition(b"\n")
        if not separator or remainder.strip():
            raise RuntimeError("invalid control response framing")
        result = json.loads(line.decode("utf-8"))
        if not isinstance(result, dict):
            raise RuntimeError("control response must be an object")
        return result

    def _owner_changed_while_attached(
        self,
        task_id: str,
        attempted_owner: int | None,
    ) -> bool:
        state = self.store.load(task_id)
        current_owner = self._owner_pid(state)
        return (
            state.get("wait_state") == WaitState.ATTACHED.value
            and current_owner is not None
            and current_owner != attempted_owner
        )

    def _offline_status(self, task_id: str) -> dict:
        return {
            "ok": True,
            "mode": "offline",
            "result": self.store.load(task_id),
        }

    def _offline_cancel(self, task_id: str) -> dict:
        state = self.store.load(task_id)
        if state.get("wait_state") != WaitState.ATTACHED.value:
            return {
                "ok": False,
                "mode": "offline",
                "error": "no-active-stale-wait",
                "result": state,
            }
        owner_pid = (state.get("wait") or {}).get("owner_pid")
        if self._pid_is_alive(owner_pid):
            return {
                "ok": False,
                "mode": "offline",
                "error": "wait-owner-still-alive",
                "result": state,
            }

        def repair(current: dict) -> None:
            wait = dict(current.get("wait") or {})
            wait["disconnect_reason"] = "offline-stale"
            wait["detached_at"] = utc_now()
            current["wait"] = wait
            current["wait_state"] = WaitState.DETACHED.value

        repaired = self.store.update(task_id, repair)
        return {
            "ok": True,
            "mode": "offline",
            "result": repaired,
        }

    def _offline_abort(self, task_id: str) -> dict:
        def persist_intent(state: dict) -> None:
            state["abort_intent"] = {
                "state": "REQUESTED",
                "requested_at": utc_now(),
                "source": "offline-control",
            }

        self.store.update(task_id, persist_intent)
        try:
            result = BridgeService(self.state_root).abort_task(task_id)
        except Exception as error:
            return {
                "ok": False,
                "mode": "offline",
                "error": "abort-unavailable",
                "detail": {"type": type(error).__name__, "message": str(error)},
            }
        return {
            "ok": result.get("outcome") == "ABORTED",
            "mode": "offline",
            "result": result,
            **(
                {}
                if result.get("outcome") == "ABORTED"
                else {"error": "abort-unavailable"}
            ),
        }

    def request(self, action: str, task_id: str) -> dict:
        if action not in ALLOWED_ACTIONS:
            raise ValueError(f"unsupported control action: {action}")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task_id is required")
        attempts = 2 if action == "cancel-wait" else 1
        for attempt in range(attempts):
            endpoint = self._online_endpoint(task_id)
            attempted_owner = endpoint[2]
            try:
                result = self._online_request(action, task_id, endpoint)
            except (ConnectionError, FileNotFoundError, socket.timeout, OSError):
                if (
                    attempt + 1 < attempts
                    and self._owner_changed_while_attached(task_id, attempted_owner)
                ):
                    continue
                break
            if (
                attempt + 1 < attempts
                and not (result.get("result") or {}).get("cancelled", False)
                and self._owner_changed_while_attached(task_id, attempted_owner)
            ):
                continue
            return result
        if action == "status":
            return self._offline_status(task_id)
        if action == "cancel-wait":
            return self._offline_cancel(task_id)
        return self._offline_abort(task_id)


def _default_state_root() -> Path:
    return resolve_state_roots(os.environ)[0]


def _resolve_task_id(state_root: Path, explicit: str | None) -> str:
    if explicit:
        return explicit
    store = TaskStore(state_root)
    if not store.tasks_root.is_dir():
        raise ValueError("no OpenCode orchestration tasks were found")
    active = []
    for task_dir in store.tasks_root.iterdir():
        if not task_dir.is_dir() or not (task_dir / "state.json").is_file():
            continue
        state = store.load(task_dir.name)
        if state.get("execution_state") not in {"COMPLETED", "FAILED", "ABORTED"}:
            active.append((state.get("updated_at", ""), task_dir.name))
    if len(active) == 1:
        return active[0][1]
    if not active:
        raise ValueError("no active OpenCode orchestration task was found; pass --task-id")
    task_ids = ", ".join(task_id for _, task_id in sorted(active, reverse=True))
    raise ValueError(f"multiple active tasks require --task-id: {task_ids}")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="oc-control")
    root.add_argument(
        "action",
        choices=("status", "cancel-wait", "abort-task"),
    )
    root.add_argument("--task-id")
    root.add_argument("--state-root", type=Path, default=_default_state_root())
    return root


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = parser().parse_args(argv)
        task_id = _resolve_task_id(arguments.state_root, arguments.task_id)
        result = ControlClient(arguments.state_root).request(arguments.action, task_id)
    except Exception as error:
        result = {
            "ok": False,
            "error": type(error).__name__,
            "message": str(error),
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
