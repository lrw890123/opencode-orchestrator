from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from collections.abc import Callable
import json
import math
import sys
import threading
import time
from typing import TextIO

from . import __version__
from .tool_service import ToolInputError
from .tools import TOOL_DEFINITIONS


JSONRPC_VERSION = "2.0"
PROTOCOL_VERSION = "2025-06-18"


class MCPProtocolServer:
    def __init__(
        self,
        tool_service,
        coordinator,
        *,
        output_stream: TextIO | None = None,
        max_workers: int = 8,
        on_initialized: Callable[[], None] | None = None,
        on_shutdown: Callable[[], None] | None = None,
        progress_interval_seconds: float = 2.0,
    ) -> None:
        self.tool_service = tool_service
        self.coordinator = coordinator
        self.output_stream = output_stream
        self._output_lock = threading.Lock()
        self._state = threading.Condition()
        self._pending: dict[tuple[type, object], tuple[object, Future]] = {}
        self._suppressed: set[tuple[type, object]] = set()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="opencode-mcp",
        )
        self._initialize_received = False
        self._initialized = False
        self._shutdown = False
        self._on_initialized = on_initialized
        self._on_shutdown = on_shutdown
        self._initialized_callback_run = False
        self._progress_interval_seconds = max(0.0, float(progress_interval_seconds))

    @staticmethod
    def _id_key(request_id: object) -> tuple[type, object]:
        return type(request_id), request_id

    def _write(self, message: dict) -> None:
        if self.output_stream is None:
            raise RuntimeError("MCP output stream is not configured")
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._output_lock:
            self.output_stream.write(payload)
            self.output_stream.write("\n")
            self.output_stream.flush()

    def _result(self, request_id: object, result: dict) -> None:
        self._write({"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result})

    def _error(
        self,
        request_id: object,
        code: int,
        message: str,
        data: dict | None = None,
    ) -> None:
        error = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        self._write({"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error})

    @staticmethod
    def _tool_result(result: dict, *, is_error: bool = False) -> dict:
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                }
            ],
            "structuredContent": result,
            "isError": is_error,
        }

    @staticmethod
    def _progress_token(params: dict) -> str | int | float | None:
        metadata = params.get("_meta")
        if not isinstance(metadata, dict):
            return None
        token = metadata.get("progressToken")
        if isinstance(token, bool):
            return None
        if isinstance(token, str):
            return token if 1 <= len(token) <= 256 else None
        if isinstance(token, int):
            return token
        if isinstance(token, float) and math.isfinite(token):
            return token
        return None

    def _progress_reporter(self, token: str | int | float | None):
        if token is None:
            return None
        lock = threading.Lock()
        sequence = 0
        last_message: str | None = None
        last_sent_at: float | None = None

        def report(message: str) -> None:
            nonlocal sequence, last_message, last_sent_at
            if not isinstance(message, str):
                return
            visible = message.strip()
            if not visible or len(visible) > 256:
                return
            now = time.monotonic()
            with lock:
                if visible == last_message:
                    return
                if (
                    last_sent_at is not None
                    and now - last_sent_at < self._progress_interval_seconds
                ):
                    return
                sequence += 1
                last_message = visible
                last_sent_at = now
                self._write(
                    {
                        "jsonrpc": JSONRPC_VERSION,
                        "method": "notifications/progress",
                        "params": {
                            "progressToken": token,
                            "progress": sequence,
                            "message": visible,
                        },
                    }
                )

        return report

    def _complete_tool(
        self,
        key: tuple[type, object],
        request_id: object,
        future: Future,
    ) -> None:
        response: tuple[str, dict | tuple[int, str, dict | None]]
        try:
            result = future.result()
            response = ("result", self._tool_result(result))
        except ToolInputError:
            response = (
                "error",
                (
                    -32602,
                    "Invalid tool arguments",
                    {"name": "ToolInputError", "code": "invalid_arguments"},
                ),
            )
        except Exception as error:
            domain_error = {
                "error": {
                    "name": type(error).__name__[:128],
                    "code": "internal_error",
                    "message": "Tool execution failed",
                }
            }
            response = ("result", self._tool_result(domain_error, is_error=True))

        with self._state:
            suppressed = key in self._suppressed
            self._pending.pop(key, None)
            self._suppressed.discard(key)
            if not suppressed:
                kind, payload = response
                if kind == "result":
                    self._result(request_id, payload)
                else:
                    code, message, data = payload
                    self._error(request_id, code, message, data)
            self._state.notify_all()

    def _schedule_tool(self, request_id: object, params: dict) -> None:
        if not isinstance(params, dict):
            self._error(request_id, -32602, "tools/call params must be an object")
            return
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or not name:
            self._error(request_id, -32602, "tools/call name must be a non-empty string")
            return
        if not isinstance(arguments, dict):
            self._error(request_id, -32602, "tools/call arguments must be an object")
            return
        key = self._id_key(request_id)
        progress = self._progress_reporter(self._progress_token(params))
        with self._state:
            if key in self._pending:
                self._error(request_id, -32600, "duplicate in-flight request id")
                return
            future = self._executor.submit(
                self.tool_service.call,
                name,
                arguments,
                str(request_id),
                progress,
            )
            self._pending[key] = (request_id, future)
            future.add_done_callback(
                lambda completed, pending_key=key, pending_id=request_id: self._complete_tool(
                    pending_key,
                    pending_id,
                    completed,
                )
            )

    def _cancel_request(self, params: dict) -> None:
        if not isinstance(params, dict) or "requestId" not in params:
            return
        request_id = params["requestId"]
        reason = params.get("reason")
        if not isinstance(reason, str) or not reason:
            reason = "client-cancelled"
        key = self._id_key(request_id)
        with self._state:
            if key in self._pending:
                self._suppressed.add(key)
        self.coordinator.cancel_request(str(request_id), reason)

    def handle(self, message: dict) -> None:
        if not isinstance(message, dict):
            self._error(None, -32600, "Invalid Request")
            return
        request_id = message.get("id")
        is_request = "id" in message
        if message.get("jsonrpc") != JSONRPC_VERSION or not isinstance(message.get("method"), str):
            if is_request:
                self._error(request_id, -32600, "Invalid Request")
            return
        method = message["method"]
        params = message.get("params", {})

        if method == "initialize":
            if not is_request:
                return
            if self._initialize_received:
                self._error(request_id, -32600, "Server is already initialized")
                return
            if not isinstance(params, dict):
                self._error(request_id, -32602, "initialize params must be an object")
                return
            self._initialize_received = True
            self._result(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": "opencode-orchestrator",
                        "version": __version__,
                    },
                },
            )
            return

        if method == "notifications/initialized":
            if self._initialize_received:
                self._initialized = True
                if not self._initialized_callback_run:
                    self._initialized_callback_run = True
                    if self._on_initialized is not None:
                        try:
                            self._on_initialized()
                        except Exception as error:
                            print(
                                f"opencode-orchestrator: initialization callback failed: {error}",
                                file=sys.stderr,
                            )
            return

        if method == "notifications/cancelled":
            self._cancel_request(params)
            return

        if not self._initialized:
            if is_request:
                self._error(request_id, -32002, "Server is not initialized")
            return

        if method == "ping":
            if is_request:
                self._result(request_id, {})
            return

        if method == "tools/list":
            if is_request:
                self._result(request_id, {"tools": list(TOOL_DEFINITIONS)})
            return

        if method == "tools/call":
            if is_request:
                self._schedule_tool(request_id, params)
            return

        if is_request:
            self._error(request_id, -32601, "Method not found")

    def wait_for_idle(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._state:
            while self._pending:
                if deadline is None:
                    self._state.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._state.wait(remaining)
            return True

    def _cancel_all(self, reason: str) -> None:
        with self._state:
            pending = list(self._pending.items())
            self._suppressed.update(key for key, _ in pending)
        for _, (request_id, _) in pending:
            self.coordinator.cancel_request(str(request_id), reason)

    def shutdown(self) -> None:
        with self._state:
            if self._shutdown:
                return
            self._shutdown = True
        self._cancel_all("server-shutdown")
        self._executor.shutdown(wait=True, cancel_futures=False)
        if self._on_shutdown is not None:
            try:
                self._on_shutdown()
            except Exception as error:
                print(
                    f"opencode-orchestrator: shutdown callback failed: {error}",
                    file=sys.stderr,
                )

    def run(self, input_stream: TextIO, output_stream: TextIO) -> None:
        self.output_stream = output_stream
        try:
            for line in input_stream:
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self._error(None, -32700, "Parse error")
                    continue
                if not isinstance(message, dict):
                    self._error(None, -32600, "Invalid Request")
                    continue
                self.handle(message)
        finally:
            self.shutdown()
