from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
import sys
from typing import TextIO

from .control_server import ControlServer
from .mcp_protocol import MCPProtocolServer
from .migration import migrate_state_root, migrate_task_records, resolve_state_roots
from .service import BridgeService
from .tool_service import ToolService


def prepare_state_root(environ: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    target, legacy = resolve_state_roots(values)
    explicit_target = bool(values.get("OPENCODE_ORCHESTRATOR_STATE_ROOT"))
    if (
        not explicit_target
        and legacy.is_dir()
        and not (target / "migration.json").is_file()
    ):
        migrate_state_root(legacy, target)
    target.mkdir(parents=True, exist_ok=True)
    migrate_task_records(target)
    return target


def build_protocol_server(
    environ: Mapping[str, str] | None = None,
    *,
    output_stream: TextIO | None = None,
) -> MCPProtocolServer:
    state_root = prepare_state_root(environ)
    bridge = BridgeService(state_root)
    tool_service = ToolService(bridge, bridge.wait_coordinator)
    control_server = ControlServer(state_root, tool_service)
    protocol = MCPProtocolServer(
        tool_service,
        bridge.wait_coordinator,
        output_stream=output_stream,
        on_initialized=control_server.start,
        on_shutdown=control_server.close,
    )
    protocol.control_server = control_server
    return protocol


def main(
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> int:
    source = sys.stdin if input_stream is None else input_stream
    destination = sys.stdout if output_stream is None else output_stream
    server = build_protocol_server(output_stream=destination)
    server.run(source, destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
