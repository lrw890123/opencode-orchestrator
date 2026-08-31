from __future__ import annotations

import hmac
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import threading


MAX_PAYLOAD_BYTES = 64 * 1024
ALLOWED_ACTIONS = {"status", "cancel-wait", "abort-task"}
MAX_UNIX_SOCKET_PATH_BYTES = 100


def control_socket_path(state_root: Path, owner_pid: int | None = None) -> Path:
    root = Path(state_root).expanduser().resolve()
    filename = "server.sock" if owner_pid is None else f"server-{owner_pid}.sock"
    preferred = root / "control" / filename
    if len(os.fsencode(preferred)) <= MAX_UNIX_SOCKET_PATH_BYTES:
        return preferred
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:20]
    suffix = "" if owner_pid is None else f"-{owner_pid}"
    return (
        Path("/tmp")
        / f"opencode-orchestrator-{os.getuid()}"
        / f"{digest}{suffix}.sock"
    )


def control_token_path(state_root: Path, owner_pid: int | None = None) -> Path:
    root = Path(state_root).expanduser().resolve()
    filename = "token" if owner_pid is None else f"token-{owner_pid}"
    return root / "control" / filename


class ControlServer:
    def __init__(self, state_root: Path, tool_service) -> None:
        self.state_root = Path(state_root).expanduser().resolve()
        self.tool_service = tool_service
        self.control_dir = self.state_root / "control"
        self.owner_pid = os.getpid()
        self.socket_path = control_socket_path(self.state_root, self.owner_pid)
        self.token_path = control_token_path(self.state_root, self.owner_pid)
        self.token: str | None = None
        self._socket: socket.socket | None = None
        self._socket_identity: tuple[int, int] | None = None
        self._token_identity: tuple[int, int] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._nonce_lock = threading.Lock()
        self._nonces: set[str] = set()

    @staticmethod
    def _identity(path: Path) -> tuple[int, int]:
        details = path.lstat()
        return details.st_dev, details.st_ino

    def _write_token(self, token: str) -> None:
        temporary = self.token_path.with_name(f"token.{os.getpid()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(descriptor, token.encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.token_path)
        os.chmod(self.token_path, 0o600)
        self._token_identity = self._identity(self.token_path)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self.control_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.control_dir, 0o700)
        socket_parent = self.socket_path.parent
        if socket_parent.exists() and socket_parent.is_symlink():
            raise RuntimeError(f"refusing symlinked control socket directory: {socket_parent}")
        socket_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        details = socket_parent.stat()
        if details.st_uid != os.getuid() or not stat.S_ISDIR(details.st_mode):
            raise RuntimeError(f"unsafe control socket directory: {socket_parent}")
        os.chmod(socket_parent, 0o700)
        if self.socket_path.exists() or self.socket_path.is_symlink():
            details = self.socket_path.lstat()
            if not stat.S_ISSOCK(details.st_mode) or details.st_uid != os.getuid():
                raise RuntimeError(f"refusing to replace unsafe control path: {self.socket_path}")
            self.socket_path.unlink()

        self.token = secrets.token_urlsafe(32)
        self._write_token(self.token)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            listener.listen(16)
            listener.settimeout(0.2)
        except Exception:
            listener.close()
            raise
        self._socket = listener
        self._socket_identity = self._identity(self.socket_path)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._serve,
            name="opencode-control",
            daemon=True,
        )
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            listener = self._socket
            if listener is None:
                return
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(
                target=self._handle_connection,
                args=(connection,),
                name="opencode-control-client",
                daemon=True,
            ).start()

    @staticmethod
    def _response(ok: bool, **payload) -> dict:
        return {"ok": ok, **payload}

    def _read_request(self, connection: socket.socket) -> dict:
        connection.settimeout(2)
        payload = bytearray()
        while b"\n" not in payload:
            chunk = connection.recv(4096)
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > MAX_PAYLOAD_BYTES:
                raise ValueError("payload-too-large")
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise ValueError("payload-too-large")
        line, separator, remainder = bytes(payload).partition(b"\n")
        if not separator or remainder.strip():
            raise ValueError("one newline-delimited request is required")
        request = json.loads(line.decode("utf-8"))
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        return request

    def _authenticate(self, request: dict) -> bool:
        supplied = request.get("token")
        return (
            isinstance(supplied, str)
            and self.token is not None
            and hmac.compare_digest(supplied, self.token)
        )

    def _remember_nonce(self, nonce: str) -> bool:
        with self._nonce_lock:
            if nonce in self._nonces:
                return False
            if len(self._nonces) >= 4096:
                self._nonces.clear()
            self._nonces.add(nonce)
            return True

    def _dispatch(self, request: dict) -> dict:
        if not self._authenticate(request):
            return self._response(False, error="unauthorized")
        action = request.get("action")
        task_id = request.get("task_id")
        nonce = request.get("nonce")
        if action not in ALLOWED_ACTIONS:
            return self._response(False, error="unsupported-action")
        if not isinstance(task_id, str) or not task_id:
            return self._response(False, error="task-id-required")
        if not isinstance(nonce, str) or not nonce:
            return self._response(False, error="nonce-required")
        if not self._remember_nonce(nonce):
            return self._response(False, error="replayed-nonce")
        tool_name = {
            "status": "task_status",
            "cancel-wait": "cancel_wait",
            "abort-task": "abort_task",
        }[action]
        arguments = {"task_id": task_id}
        if action == "cancel-wait":
            arguments["reason"] = "external-control"
        try:
            result = self.tool_service.call(tool_name, arguments, f"control:{nonce}")
        except Exception as error:
            return self._response(
                False,
                error="control-action-failed",
                detail={"type": type(error).__name__, "message": str(error)},
            )
        return self._response(True, result=result)

    def _handle_connection(self, connection: socket.socket) -> None:
        with connection:
            try:
                request = self._read_request(connection)
                response = self._dispatch(request)
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as error:
                response = self._response(False, error=str(error))
            try:
                connection.sendall(
                    json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    + b"\n"
                )
            except OSError:
                return

    @staticmethod
    def _unlink_if_owned(path: Path, identity: tuple[int, int] | None) -> None:
        if identity is None:
            return
        try:
            current = ControlServer._identity(path)
        except FileNotFoundError:
            return
        if current == identity:
            path.unlink()

    def close(self) -> None:
        self._stop.set()
        listener = self._socket
        self._socket = None
        if listener is not None:
            listener.close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        self._unlink_if_owned(self.socket_path, self._socket_identity)
        self._unlink_if_owned(self.token_path, self._token_identity)
        self._socket_identity = None
        self._token_identity = None
        self.token = None
