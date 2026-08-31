#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

VERSION = "0.1.0"


class UsageError(ValueError):
    pass


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise UsageError(message)


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def usage_error(message: str) -> int:
    emit({"ok": False, "error": {"code": "usage", "message": message}})
    return 2


def default_state_root() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return codex_home / "opencode-orchestrator"


def parser() -> JsonArgumentParser:
    root = JsonArgumentParser(add_help=True)
    subcommands = root.add_subparsers(dest="command", required=True)
    subcommands.add_parser("version")

    prepare = subcommands.add_parser("prepare")
    prepare.add_argument("--state-root", type=Path, default=default_state_root())
    prepare.add_argument("--repo", type=Path, required=True)
    prepare.add_argument("--slug", required=True)
    prepare.add_argument("--request", type=Path, required=True)
    prepare.add_argument("--server", default=os.environ.get("OPENCODE_SERVER_URL", "http://127.0.0.1:4096"))

    for command in ("dispatch", "wait", "status", "collect", "abort"):
        item = subcommands.add_parser(command)
        item.add_argument("--state-root", type=Path, default=default_state_root())
        item.add_argument("--task-id", required=True)
        if command in {"dispatch", "wait"}:
            item.add_argument("--timeout", type=int, default=1800)

    reply = subcommands.add_parser("reply")
    reply.add_argument("--state-root", type=Path, default=default_state_root())
    reply.add_argument("--task-id", required=True)
    reply.add_argument("--kind", choices=("review", "permission", "question"), required=True)
    reply.add_argument("--payload", type=Path, required=True)
    reply.add_argument("--timeout", type=int, default=1800)

    approve_review = subcommands.add_parser("approve-review")
    approve_review.add_argument("--state-root", type=Path, default=default_state_root())
    approve_review.add_argument("--task-id", required=True)
    approve_review.add_argument("--payload", type=Path, required=True)

    cleanup = subcommands.add_parser("cleanup")
    cleanup.add_argument("--state-root", type=Path, default=default_state_root())
    cleanup.add_argument("--task-id", required=True)
    cleanup.add_argument("--confirm", required=True)
    return root


def read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise UsageError(f"JSON input must be an object: {path}")
    return payload


def execute(arguments: argparse.Namespace) -> dict[str, Any]:
    if arguments.command == "version":
        return {"ok": True, "version": VERSION, "schema_version": 1}

    from .service import BridgeService

    service = BridgeService(arguments.state_root)
    if arguments.command == "prepare":
        result = service.prepare(
            arguments.repo,
            arguments.slug,
            read_json(arguments.request),
            arguments.server,
        )
    elif arguments.command == "dispatch":
        result = service.dispatch(arguments.task_id, arguments.timeout)
    elif arguments.command == "wait":
        result = service.wait(arguments.task_id, arguments.timeout)
    elif arguments.command == "status":
        result = service.status(arguments.task_id)
    elif arguments.command == "collect":
        result = service.collect(arguments.task_id)
    elif arguments.command == "abort":
        result = service.abort(arguments.task_id)
    elif arguments.command == "reply":
        result = service.reply(
            arguments.task_id,
            arguments.kind,
            read_json(arguments.payload),
            arguments.timeout,
        )
    elif arguments.command == "approve-review":
        result = service.approve_review(
            arguments.task_id,
            read_json(arguments.payload),
        )
    elif arguments.command == "cleanup":
        result = service.cleanup(arguments.task_id, arguments.confirm)
    else:
        raise UsageError(f"unknown command: {arguments.command}")
    result = dict(result)
    result.setdefault("ok", True)
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        return usage_error("a command is required")
    try:
        parsed = parser().parse_args(arguments)
        result = execute(parsed)
        emit(result)
        if result.get("ok", True):
            return 0
        if result.get("phase") in {"NEEDS_INPUT", "PERMISSION_WAIT"}:
            return 6
        return 5
    except UsageError as error:
        return usage_error(str(error))
    except ValueError as error:
        emit({"ok": False, "error": {"code": "policy", "message": str(error)}})
        return 3
    except Exception as error:
        emit({"ok": False, "error": {"code": "runtime", "message": str(error)}})
        print(f"oc_bridge: {error}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
