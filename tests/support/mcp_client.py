from __future__ import annotations

import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time


class MCPSubprocessClient:
    def __init__(self, script: Path, *, state_root: Path):
        environment = os.environ.copy()
        environment["OPENCODE_ORCHESTRATOR_STATE_ROOT"] = str(state_root)
        environment["PYTHONUNBUFFERED"] = "1"
        self.process = subprocess.Popen(
            [sys.executable, str(script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=environment,
        )
        self.sent_request_count = 0

    def send(self, message: dict) -> None:
        assert self.process.stdin is not None
        if "id" in message:
            self.sent_request_count += 1
        self.process.stdin.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def has_response(self, *, timeout: float = 0.0) -> bool:
        assert self.process.stdout is not None
        ready, _, _ = select.select([self.process.stdout], [], [], timeout)
        return bool(ready)

    def receive(self, *, timeout: float = 2.0) -> dict:
        assert self.process.stdout is not None
        ready, _, _ = select.select([self.process.stdout], [], [], timeout)
        if not ready:
            raise TimeoutError("timed out waiting for MCP response")
        line = self.process.stdout.readline()
        if not line:
            raise EOFError("MCP server closed stdout")
        return json.loads(line)

    def request(self, message: dict, *, timeout: float = 2.0) -> dict:
        self.send(message)
        deadline = time.monotonic() + timeout
        while True:
            response = self.receive(timeout=max(0.0, deadline - time.monotonic()))
            if response.get("id") == message.get("id"):
                return response

    def close(self) -> tuple[str, str]:
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
        self.process.wait(timeout=2)
        stdout = self.process.stdout.read() if self.process.stdout is not None else ""
        stderr = self.process.stderr.read() if self.process.stderr is not None else ""
        if self.process.stdout is not None:
            self.process.stdout.close()
        if self.process.stderr is not None:
            self.process.stderr.close()
        return stdout, stderr

    def kill(self) -> tuple[str, str]:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=2)
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
        stdout = self.process.stdout.read() if self.process.stdout is not None else ""
        stderr = self.process.stderr.read() if self.process.stderr is not None else ""
        if self.process.stdout is not None:
            self.process.stdout.close()
        if self.process.stderr is not None:
            self.process.stderr.close()
        return stdout, stderr
