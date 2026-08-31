#!/usr/bin/env python3
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from opencode_orchestrator import __version__
from opencode_orchestrator.task_state import atomic_write_json, utc_now


PLUGIN_NAME = "opencode-orchestrator"
MARKETPLACE_NAME = "opencode-orchestrator-local"
SELECTOR = f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"
REQUIRED_TOOLS = {
    "delegate_and_wait",
    "reply_and_wait",
    "resume_wait",
    "task_status",
    "read_transcript",
    "collect_result",
    "cancel_wait",
    "abort_task",
}


def _default_runner(command: list[str], codex_home: Path):
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(codex_home)
    return subprocess.run(
        command,
        text=True,
        capture_output=True,
        env=environment,
    )


def _invoke(runner: Callable, command: list[str]) -> object:
    completed = runner(command)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise RuntimeError(f"command failed ({' '.join(command)}): {detail}")
    return completed


def _json_output(completed: object) -> object:
    output = completed.stdout.strip()
    if not output:
        return {}
    try:
        return json.loads(output)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"command did not return JSON: {output}") from error


def _read_record(record_path: Path) -> dict:
    with Path(record_path).expanduser().resolve().open(encoding="utf-8") as handle:
        record = json.load(handle)
    if record.get("schema_version") != 1:
        raise ValueError("unsupported install record")
    return record


def _write_record(record_path: Path, record: dict) -> None:
    saved = deepcopy(record)
    saved["updated_at"] = utc_now()
    atomic_write_json(Path(record_path).expanduser().resolve(), saved)


def _tree_digest(root: Path) -> str | None:
    directory = Path(root)
    if not directory.exists() and not directory.is_symlink():
        return None
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        relative = str(path.relative_to(directory))
        digest.update(relative.encode("utf-8"))
        if path.is_symlink():
            digest.update(b"L")
            digest.update(os.readlink(path).encode("utf-8"))
        elif path.is_file():
            digest.update(b"F")
            digest.update(path.read_bytes())
        elif path.is_dir():
            digest.update(b"D")
    return f"sha256:{digest.hexdigest()}"


def _inside(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative.startswith("./"):
        raise ValueError(f"Plugin path must start with ./: {relative}")
    resolved_root = root.resolve()
    candidate = (resolved_root / relative).resolve()
    if not candidate.is_relative_to(resolved_root):
        raise ValueError(f"Plugin path escapes root: {relative}")
    return candidate


def _validate_plugin(plugin_root: Path) -> dict:
    root = Path(plugin_root).expanduser().resolve()
    manifest_path = root / ".codex-plugin/plugin.json"
    mcp_path = root / ".mcp.json"
    marketplace_path = root / ".agents/plugins/marketplace.json"
    for path in (manifest_path, mcp_path, marketplace_path):
        if not path.is_file():
            raise ValueError(f"required Plugin file is missing: {path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("name") != PLUGIN_NAME or manifest.get("version") != __version__:
        raise ValueError(
            f"Plugin identity must be opencode-orchestrator {__version__}"
        )
    skills = _inside(root, manifest.get("skills"))
    mcp_reference = _inside(root, manifest.get("mcpServers"))
    if not skills.is_dir() or not (skills / PLUGIN_NAME / "SKILL.md").is_file():
        raise ValueError("Plugin Skill entrypoint is missing")
    if mcp_reference != mcp_path:
        raise ValueError("Plugin MCP reference must point to ./.mcp.json")
    mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
    server = (mcp.get("mcpServers") or {}).get(PLUGIN_NAME)
    if not isinstance(server, dict):
        raise ValueError("bundled MCP server declaration is missing")
    if server.get("tool_timeout_sec") != 90000:
        raise ValueError("bundled MCP tool_timeout_sec must equal 90000")
    if server.get("command") != "python3" or server.get("cwd") != ".":
        raise ValueError("bundled MCP command and cwd must remain relative")
    if server.get("args") != ["./mcp/server.py"]:
        raise ValueError("bundled MCP entrypoint must be ./mcp/server.py")
    entrypoint = _inside(root, server["args"][0])
    if not entrypoint.is_file():
        raise ValueError("bundled MCP entrypoint does not exist")
    marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
    if marketplace.get("name") != MARKETPLACE_NAME:
        raise ValueError(f"marketplace name must be {MARKETPLACE_NAME}")
    entries = marketplace.get("plugins") or []
    if len(entries) != 1 or entries[0].get("name") != PLUGIN_NAME:
        raise ValueError("marketplace must contain exactly the orchestrator Plugin")
    if entries[0].get("source") != {"source": "local", "path": "./"}:
        raise ValueError("marketplace source must point at the Plugin root")
    return {
        "root": root,
        "manifest": manifest,
        "server": server,
        "skill_path": skills / PLUGIN_NAME,
        "tool_timeout_sec": server["tool_timeout_sec"],
    }


def _copy_runtime(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True)
    for relative in (
        ".codex-plugin",
        ".mcp.json",
        "bin",
        "mcp",
        "skills",
        "src",
    ):
        origin = source / relative
        target = destination / relative
        if origin.is_dir():
            shutil.copytree(
                origin,
                target,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )
        else:
            shutil.copy2(origin, target)


def _read_response(process: subprocess.Popen, request_id: object, timeout: float = 3) -> dict:
    assert process.stdout is not None
    ready, _, _ = select.select([process.stdout], [], [], timeout)
    if not ready:
        raise RuntimeError(f"MCP handshake timed out waiting for request {request_id}")
    line = process.stdout.readline()
    if not line:
        raise RuntimeError("MCP server closed stdout during handshake")
    response = json.loads(line)
    if response.get("id") != request_id:
        raise RuntimeError(f"unexpected MCP response id: {response.get('id')}")
    if "error" in response:
        raise RuntimeError(f"MCP handshake returned an error: {response['error']}")
    return response


def _handshake(plugin_root: Path) -> None:
    with TemporaryDirectory() as tmp:
        temporary = Path(tmp)
        copied = temporary / PLUGIN_NAME
        _copy_runtime(plugin_root, copied)
        configured = json.loads((copied / ".mcp.json").read_text(encoding="utf-8"))[
            "mcpServers"
        ][PLUGIN_NAME]
        environment = os.environ.copy()
        environment["OPENCODE_ORCHESTRATOR_STATE_ROOT"] = str(temporary / "state")
        process = subprocess.Popen(
            [configured["command"], *configured["args"]],
            cwd=copied / configured["cwd"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=environment,
        )
        try:
            assert process.stdin is not None
            initialize = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "installer", "version": "1"},
                },
            }
            process.stdin.write(json.dumps(initialize, separators=(",", ":")) + "\n")
            process.stdin.flush()
            initialized = _read_response(process, 1)
            if initialized["result"].get("protocolVersion") != "2025-06-18":
                raise RuntimeError("MCP server negotiated an unexpected protocol version")
            process.stdin.write(
                '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
            )
            process.stdin.write(
                '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n'
            )
            process.stdin.flush()
            listed = _read_response(process, 2)
            names = {tool["name"] for tool in listed["result"].get("tools", [])}
            if names != REQUIRED_TOOLS:
                raise RuntimeError(f"MCP tool list mismatch: {sorted(names)}")
            process.stdin.close()
            process.wait(timeout=3)
            if process.returncode != 0:
                assert process.stderr is not None
                raise RuntimeError(f"MCP handshake process failed: {process.stderr.read()}")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()


def _marketplaces(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        values = payload.get("marketplaces", [])
        return [item for item in values if isinstance(item, dict)]
    return []


def _installed_plugins(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        values = payload.get("installed", [])
        return [item for item in values if isinstance(item, dict)]
    return []


def _marketplace_root(entry: dict) -> Path | None:
    value = entry.get("root") or entry.get("path")
    return Path(value).expanduser().resolve() if isinstance(value, str) else None


def _find_marketplace(payload: object) -> dict | None:
    return next(
        (entry for entry in _marketplaces(payload) if entry.get("name") == MARKETPLACE_NAME),
        None,
    )


def _plugin_is_installed(payload: object) -> bool:
    return _find_installed_plugin(payload) is not None


def _find_installed_plugin(payload: object) -> dict | None:
    for plugin in _installed_plugins(payload):
        plugin_id = plugin.get("pluginId")
        marketplace = plugin.get("marketplaceName") or plugin.get("marketplace")
        if plugin_id == SELECTOR or (
            plugin.get("name") == PLUGIN_NAME
            and marketplace == MARKETPLACE_NAME
            and plugin.get("installed", True)
        ):
            return plugin
    return None


def _snapshot_previous_plugin(
    home: Path,
    record_path: Path,
    plugin_entry: dict,
) -> dict:
    version = plugin_entry.get("version")
    if not isinstance(version, str) or not version.strip() or version != version.strip():
        raise RuntimeError("installed Plugin version is unavailable for rollback")
    cache_path = (
        home
        / "plugins/cache"
        / MARKETPLACE_NAME
        / PLUGIN_NAME
        / version
    ).resolve()
    cache_root = (home / "plugins/cache").resolve()
    if not cache_path.is_relative_to(cache_root) or not cache_path.is_dir():
        raise RuntimeError("installed Plugin cache is unavailable for rollback")
    manifest_path = cache_path / ".codex-plugin/plugin.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError) as error:
        raise RuntimeError("installed Plugin cache manifest is invalid") from error
    if manifest.get("name") != PLUGIN_NAME or manifest.get("version") != version:
        raise RuntimeError("installed Plugin cache identity does not match plugin list")
    if not (cache_path / ".agents/plugins/marketplace.json").is_file():
        raise RuntimeError("installed Plugin cache lacks marketplace metadata")

    snapshot = record_path.parent / "snapshots/previous-plugin-root"
    if snapshot.exists() or snapshot.is_symlink():
        raise FileExistsError(f"previous Plugin snapshot already exists: {snapshot}")
    fingerprint = _tree_digest(cache_path)
    if fingerprint is None:
        raise RuntimeError("installed Plugin cache fingerprint is unavailable")
    return {
        "path": str(cache_path),
        "version": version,
        "fingerprint": fingerprint,
        "snapshot_path": str(snapshot),
        "snapshot_fingerprint": None,
        "state": "INTENT",
    }


def _copy_previous_plugin_snapshot(cache: dict) -> dict:
    cache_path = Path(cache["path"]).resolve()
    snapshot = Path(cache["snapshot_path"]).resolve()
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(cache_path, snapshot, symlinks=True)
    snapshot_fingerprint = _tree_digest(snapshot)
    if snapshot_fingerprint != cache.get("fingerprint"):
        raise RuntimeError("previous Plugin snapshot verification failed")
    completed = deepcopy(cache)
    completed["snapshot_fingerprint"] = snapshot_fingerprint
    completed["state"] = "APPLIED"
    completed["completed_at"] = utc_now()
    return completed


def _mutation(record: dict, kind: str) -> dict | None:
    return next((item for item in record.get("mutations", []) if item.get("kind") == kind), None)


def _set_mutation(record_path: Path, record: dict, kind: str, **updates) -> dict:
    mutation = _mutation(record, kind)
    if mutation is None:
        mutation = {"kind": kind, "state": "INTENT", "recorded_at": utc_now()}
        record.setdefault("mutations", []).append(mutation)
    mutation.update(updates)
    _write_record(record_path, record)
    return mutation


def preinstall(
    plugin_root: Path,
    codex_home: Path,
    codex_bin: str,
    runner: Callable | None = None,
    record_path: Path | None = None,
) -> dict:
    plugin = _validate_plugin(plugin_root)
    home = Path(codex_home).expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True)
    record = (
        Path(record_path).expanduser().resolve()
        if record_path is not None
        else home / "plugin-data/opencode-orchestrator/install.json"
    )
    if record.exists() or record.is_symlink():
        raise FileExistsError(f"install record already exists: {record}")
    execute = runner or (lambda command: _default_runner(command, home))
    old_skill = home / "skills" / PLUGIN_NAME
    plugin_list_before = _json_output(
        _invoke(execute, [codex_bin, "plugin", "list", "--json"])
    )
    marketplace_list_before = _json_output(
        _invoke(execute, [codex_bin, "plugin", "marketplace", "list", "--json"])
    )
    existing_marketplace = _find_marketplace(marketplace_list_before)
    existing_plugin = _find_installed_plugin(plugin_list_before)
    if existing_marketplace is not None:
        existing_root = _marketplace_root(existing_marketplace)
        if existing_root is not None and existing_root != plugin["root"]:
            raise ValueError(
                f"marketplace {MARKETPLACE_NAME} already points at {existing_root}"
            )
    _handshake(plugin["root"])
    previous_cache = None
    if existing_plugin is not None:
        if existing_marketplace is None:
            raise RuntimeError("installed Plugin has no recoverable marketplace source")
        previous_cache = _snapshot_previous_plugin(home, record, existing_plugin)
    install = {
        "schema_version": 1,
        "status": "PREINSTALLING",
        "created_at": utc_now(),
        "plugin": {
            "name": PLUGIN_NAME,
            "version": plugin["manifest"]["version"],
            "root": str(plugin["root"]),
            "marketplace": MARKETPLACE_NAME,
            "selector": SELECTOR,
            "tool_timeout_sec": plugin["tool_timeout_sec"],
            "skill_fingerprint": _tree_digest(plugin["skill_path"]),
        },
        "codex_home": str(home),
        "codex_bin": codex_bin,
        "record_path": str(record),
        "old_skill": {
            "path": str(old_skill),
            "existed": old_skill.exists() or old_skill.is_symlink(),
            "fingerprint": _tree_digest(old_skill),
        },
        "previous": {
            "marketplace": existing_marketplace,
            "plugin_installed": existing_plugin is not None,
            "plugin": deepcopy(existing_plugin),
            "cache": previous_cache,
        },
        "checks": {
            "paths": True,
            "mcp_handshake": True,
        },
        "mutations": [],
    }
    _write_record(record, install)
    if previous_cache is not None:
        install["previous"]["cache"] = _copy_previous_plugin_snapshot(
            previous_cache
        )
        _write_record(record, install)
    if existing_marketplace is None:
        mutation = _set_mutation(
            record,
            install,
            "marketplace_add",
            command=[codex_bin, "plugin", "marketplace", "add", str(plugin["root"])],
        )
        _invoke(execute, mutation["command"])
        mutation["state"] = "APPLIED"
        mutation["completed_at"] = utc_now()
        _write_record(record, install)
    verified_marketplaces = _json_output(
        _invoke(execute, [codex_bin, "plugin", "marketplace", "list", "--json"])
    )
    if _find_marketplace(verified_marketplaces) is None:
        install["status"] = "PREINSTALL_FAILED"
        install["failure"] = "marketplace registration was not visible"
        _write_record(record, install)
        raise RuntimeError(install["failure"])
    install["status"] = "PREINSTALLED"
    install["preinstalled_at"] = utc_now()
    _write_record(record, install)
    return deepcopy(install)


def _load_for_mutation(record_path: Path) -> tuple[Path, dict, Callable]:
    path = Path(record_path).expanduser().resolve()
    record = _read_record(path)
    home = Path(record["codex_home"]).resolve()
    runner = lambda command: _default_runner(command, home)
    return path, record, runner


def activate(record_path: Path, *, runner: Callable | None = None) -> dict:
    path, record, default_runner = _load_for_mutation(record_path)
    execute = runner or default_runner
    if record.get("status") != "PREINSTALLED":
        raise ValueError(f"activation requires PREINSTALLED record, got {record.get('status')}")
    codex_bin = record["codex_bin"]
    marketplaces = _json_output(
        _invoke(execute, [codex_bin, "plugin", "marketplace", "list", "--json"])
    )
    if _find_marketplace(marketplaces) is None:
        raise RuntimeError(f"marketplace {MARKETPLACE_NAME} is not registered")
    try:
        plugin_add = _set_mutation(
            path,
            record,
            "plugin_add",
            command=[codex_bin, "plugin", "add", SELECTOR, "--json"],
        )
        _invoke(execute, plugin_add["command"])
        plugin_add["state"] = "APPLIED"
        plugin_add["completed_at"] = utc_now()
        _write_record(path, record)
        listed = _json_output(
            _invoke(execute, [codex_bin, "plugin", "list", "--json"])
        )
        if not _plugin_is_installed(listed):
            raise RuntimeError("installed Plugin was not visible in codex plugin list")

        old_skill = Path(record["old_skill"]["path"])
        if record["old_skill"]["existed"]:
            home = Path(record["codex_home"]).resolve()
            skills_root = old_skill.parent.resolve()
            if not skills_root.is_relative_to(home) or old_skill.is_symlink():
                raise ValueError(f"unsafe old Skill path: {old_skill}")
            backup = path.parent / "backup/skill"
            if backup.exists() or backup.is_symlink():
                raise FileExistsError(f"Skill backup already exists: {backup}")
            backup.parent.mkdir(parents=True, exist_ok=True)
            skill_move = _set_mutation(
                path,
                record,
                "skill_move",
                source=str(old_skill),
                destination=str(backup),
                original_fingerprint=record["old_skill"]["fingerprint"],
            )
            os.replace(old_skill, backup)
            skill_move["state"] = "APPLIED"
            skill_move["completed_at"] = utc_now()
            _write_record(path, record)

        final_list = _json_output(
            _invoke(execute, [codex_bin, "plugin", "list", "--json"])
        )
        if not _plugin_is_installed(final_list):
            raise RuntimeError("Plugin disappeared during final activation verification")
    except Exception as error:
        record["status"] = "ACTIVATION_FAILED"
        record["failure"] = {"type": type(error).__name__, "message": str(error)}
        _write_record(path, record)
        raise
    record["status"] = "ACTIVATED"
    record["activated_at"] = utc_now()
    record["restart_required"] = True
    _write_record(path, record)
    return deepcopy(record)


def _recorded_restore_step(
    path: Path,
    record: dict,
    mutation: dict,
    execute: Callable,
    label: str,
    command: list[str],
) -> None:
    steps = mutation.setdefault("steps", [])
    step = next(
        (
            candidate
            for candidate in reversed(steps)
            if candidate.get("label") == label
            and candidate.get("state") == "INTENT"
        ),
        None,
    )
    if step is None:
        step = {
            "label": label,
            "state": "INTENT",
            "command": command,
            "recorded_at": utc_now(),
        }
        steps.append(step)
        _write_record(path, record)
    elif step.get("command") != command:
        raise RuntimeError(f"restore journal command mismatch for {label}")
    _invoke(execute, command)
    step["state"] = "APPLIED"
    step["completed_at"] = utc_now()
    _write_record(path, record)


def _previous_plugin_is_restored(
    record: dict,
    plugin_payload: object,
    marketplace_payload: object,
) -> bool:
    previous = record.get("previous") or {}
    plugin = previous.get("plugin")
    cache = previous.get("cache")
    marketplace = previous.get("marketplace")
    current = _find_installed_plugin(plugin_payload)
    current_marketplace = _find_marketplace(marketplace_payload)
    if not all(isinstance(item, dict) for item in (plugin, cache, marketplace)):
        return False
    if current is None or current_marketplace is None:
        return False
    if current.get("version") != plugin.get("version"):
        return False
    previous_marketplace_root = _marketplace_root(marketplace)
    if (
        previous_marketplace_root is None
        or _marketplace_root(current_marketplace) != previous_marketplace_root
    ):
        return False
    cache_path = cache.get("path")
    fingerprint = cache.get("fingerprint")
    return (
        isinstance(cache_path, str)
        and isinstance(fingerprint, str)
        and _tree_digest(Path(cache_path)) == fingerprint
    )


def _prior_plugin_version_is_installed(record: dict, plugin_payload: object) -> bool:
    previous = record.get("previous") or {}
    plugin = previous.get("plugin")
    cache = previous.get("cache")
    current = _find_installed_plugin(plugin_payload)
    if not isinstance(plugin, dict) or not isinstance(cache, dict) or current is None:
        return False
    return (
        current.get("version") == plugin.get("version")
        and isinstance(cache.get("path"), str)
        and isinstance(cache.get("fingerprint"), str)
        and _tree_digest(Path(cache["path"])) == cache["fingerprint"]
    )


def _reconcile_restore_intents(
    path: Path,
    record: dict,
    mutation: dict,
    plugin_payload: object,
    marketplace_payload: object,
    snapshot: Path,
    previous_marketplace_root: Path,
) -> None:
    current_plugin = _find_installed_plugin(plugin_payload)
    current_marketplace = _find_marketplace(marketplace_payload)
    current_root = (
        _marketplace_root(current_marketplace)
        if current_marketplace is not None
        else None
    )
    prior_installed = _prior_plugin_version_is_installed(record, plugin_payload)
    reconciled = False
    for step in mutation.get("steps", []):
        if step.get("state") != "INTENT":
            continue
        label = step.get("label")
        applied = (
            (label == "remove-current-plugin" and current_plugin is None)
            or (label == "remove-current-marketplace" and current_marketplace is None)
            or (label == "add-snapshot-marketplace" and current_root == snapshot)
            or (label == "install-previous-plugin" and prior_installed)
            or (label == "remove-snapshot-marketplace" and current_marketplace is None)
            or (
                label == "restore-marketplace-source"
                and current_root == previous_marketplace_root
            )
        )
        if applied:
            step["state"] = "APPLIED"
            step["completed_at"] = utc_now()
            step["reconciled_from"] = "actual-state"
            reconciled = True
    if reconciled:
        _write_record(path, record)


def _verify_restore_journal(
    mutation: dict,
    codex_bin: str,
    snapshot: Path,
    previous_marketplace_root: Path,
) -> None:
    expected = {
        "remove-current-plugin": [
            codex_bin,
            "plugin",
            "remove",
            SELECTOR,
            "--json",
        ],
        "remove-current-marketplace": [
            codex_bin,
            "plugin",
            "marketplace",
            "remove",
            MARKETPLACE_NAME,
        ],
        "add-snapshot-marketplace": [
            codex_bin,
            "plugin",
            "marketplace",
            "add",
            str(snapshot),
        ],
        "install-previous-plugin": [
            codex_bin,
            "plugin",
            "add",
            SELECTOR,
            "--json",
        ],
        "remove-snapshot-marketplace": [
            codex_bin,
            "plugin",
            "marketplace",
            "remove",
            MARKETPLACE_NAME,
        ],
        "restore-marketplace-source": [
            codex_bin,
            "plugin",
            "marketplace",
            "add",
            str(previous_marketplace_root),
        ],
    }
    for step in mutation.get("steps", []):
        label = step.get("label")
        if label not in expected or step.get("command") != expected[label]:
            raise RuntimeError("restore journal contains an invalid step")


def _complete_restore_journal(path: Path, record: dict, mutation: dict) -> None:
    for step in mutation.get("steps", []):
        if step.get("state") == "INTENT":
            # A verified final state proves an interrupted intermediate action
            # is no longer outstanding even when its transient effect is gone.
            step["state"] = "APPLIED"
            step["completed_at"] = utc_now()
            step["reconciled_from"] = "verified-final-state"
    mutation["state"] = "APPLIED"
    mutation["completed_at"] = utc_now()
    _write_record(path, record)


def _restore_previous_plugin(
    path: Path,
    record: dict,
    execute: Callable,
) -> None:
    previous = record["previous"]
    plugin = previous.get("plugin")
    cache = previous.get("cache")
    marketplace = previous.get("marketplace")
    if not all(isinstance(item, dict) for item in (plugin, cache, marketplace)):
        raise RuntimeError("previous Plugin rollback snapshot is incomplete")
    snapshot = Path(cache["snapshot_path"]).resolve()
    snapshots_root = (path.parent / "snapshots").resolve()
    if (
        not snapshot.is_relative_to(snapshots_root)
        or not snapshot.is_dir()
        or _tree_digest(snapshot) != cache.get("snapshot_fingerprint")
    ):
        raise RuntimeError("previous Plugin rollback snapshot failed verification")
    previous_marketplace_root = _marketplace_root(marketplace)
    if previous_marketplace_root is None or not previous_marketplace_root.is_dir():
        raise RuntimeError("previous marketplace source is unavailable")

    mutation = _set_mutation(
        path,
        record,
        "plugin_restore",
        previous_version=plugin.get("version"),
        previous_marketplace_root=str(previous_marketplace_root),
        snapshot_path=str(snapshot),
        snapshot_fingerprint=cache.get("snapshot_fingerprint"),
    )
    codex_bin = record["codex_bin"]
    _verify_restore_journal(
        mutation,
        codex_bin,
        snapshot,
        previous_marketplace_root,
    )
    installed = _json_output(
        _invoke(execute, [codex_bin, "plugin", "list", "--json"])
    )
    marketplaces = _json_output(
        _invoke(execute, [codex_bin, "plugin", "marketplace", "list", "--json"])
    )
    _reconcile_restore_intents(
        path,
        record,
        mutation,
        installed,
        marketplaces,
        snapshot,
        previous_marketplace_root,
    )

    if _previous_plugin_is_restored(record, installed, marketplaces):
        _complete_restore_journal(path, record, mutation)
        return

    prior_installed = _prior_plugin_version_is_installed(record, installed)
    current_marketplace = _find_marketplace(marketplaces)
    current_marketplace_root = (
        _marketplace_root(current_marketplace)
        if current_marketplace is not None
        else None
    )

    if not prior_installed and _find_installed_plugin(installed) is not None:
        _recorded_restore_step(
            path,
            record,
            mutation,
            execute,
            "remove-current-plugin",
            [codex_bin, "plugin", "remove", SELECTOR, "--json"],
        )
    if current_marketplace is not None and current_marketplace_root != previous_marketplace_root:
        removal_label = (
            "remove-snapshot-marketplace"
            if current_marketplace_root == snapshot
            else "remove-current-marketplace"
        )
        _recorded_restore_step(
            path,
            record,
            mutation,
            execute,
            removal_label,
            [codex_bin, "plugin", "marketplace", "remove", MARKETPLACE_NAME],
        )

    if not prior_installed:
        if current_marketplace_root == previous_marketplace_root:
            _recorded_restore_step(
                path,
                record,
                mutation,
                execute,
                "remove-current-marketplace",
                [codex_bin, "plugin", "marketplace", "remove", MARKETPLACE_NAME],
            )
        _recorded_restore_step(
            path,
            record,
            mutation,
            execute,
            "add-snapshot-marketplace",
            [codex_bin, "plugin", "marketplace", "add", str(snapshot)],
        )
        _recorded_restore_step(
            path,
            record,
            mutation,
            execute,
            "install-previous-plugin",
            [codex_bin, "plugin", "add", SELECTOR, "--json"],
        )
        _recorded_restore_step(
            path,
            record,
            mutation,
            execute,
            "remove-snapshot-marketplace",
            [codex_bin, "plugin", "marketplace", "remove", MARKETPLACE_NAME],
        )
    _recorded_restore_step(
        path,
        record,
        mutation,
        execute,
        "restore-marketplace-source",
        [
            codex_bin,
            "plugin",
            "marketplace",
            "add",
            str(previous_marketplace_root),
        ],
    )
    final_plugins = _json_output(
        _invoke(execute, [codex_bin, "plugin", "list", "--json"])
    )
    final_marketplaces = _json_output(
        _invoke(execute, [codex_bin, "plugin", "marketplace", "list", "--json"])
    )
    final_marketplace = _find_marketplace(final_marketplaces)
    if not _previous_plugin_is_restored(record, final_plugins, final_marketplaces):
        if _prior_plugin_version_is_installed(record, final_plugins):
            raise RuntimeError("previous marketplace source was not restored")
        raise RuntimeError("previous Plugin version or cache was not restored")
    if final_marketplace is None:
        raise RuntimeError("previous marketplace source was not restored")
    _complete_restore_journal(path, record, mutation)


def _rollback_skill_move(path: Path, record: dict, skill_move: dict) -> None:
    source = Path(skill_move["source"])
    backup = Path(skill_move["destination"])
    original = skill_move.get("original_fingerprint")
    if not backup.is_dir():
        if source.is_dir() and _tree_digest(source) == original:
            skill_move["state"] = "ROLLED_BACK"
            skill_move["rolled_back_at"] = utc_now()
            skill_move["reconciled_from"] = "source"
            _write_record(path, record)
            return
        raise FileNotFoundError("recorded Skill backup is missing")
    if source.exists() or source.is_symlink():
        actual = _tree_digest(source)
        expected = record["plugin"]["skill_fingerprint"]
        if actual != expected:
            raise RuntimeError("refusing to overwrite a changed Skill during rollback")
        displaced = path.parent / "backup/activated-skill"
        if displaced.exists() or displaced.is_symlink():
            raise FileExistsError("activated Skill backup already exists")
        os.replace(source, displaced)
        skill_move["displaced_plugin_skill"] = str(displaced)
    source.parent.mkdir(parents=True, exist_ok=True)
    os.replace(backup, source)
    if _tree_digest(source) != original:
        raise RuntimeError("restored Skill fingerprint does not match the backup")
    skill_move["state"] = "ROLLED_BACK"
    skill_move["rolled_back_at"] = utc_now()
    _write_record(path, record)


def rollback(record_path: Path, *, runner: Callable | None = None) -> dict:
    path, record, default_runner = _load_for_mutation(record_path)
    execute = runner or default_runner
    codex_bin = record["codex_bin"]
    try:
        skill_move = _mutation(record, "skill_move")
        if skill_move is not None and skill_move.get("state") in {"INTENT", "APPLIED"}:
            _rollback_skill_move(path, record, skill_move)

        plugin_add = _mutation(record, "plugin_add")
        if plugin_add is not None and plugin_add.get("state") in {"INTENT", "APPLIED"}:
            listed = _json_output(
                _invoke(execute, [codex_bin, "plugin", "list", "--json"])
            )
            if record["previous"].get("plugin_installed"):
                marketplaces = _json_output(
                    _invoke(
                        execute,
                        [codex_bin, "plugin", "marketplace", "list", "--json"],
                    )
                )
                if not _previous_plugin_is_restored(record, listed, marketplaces):
                    _restore_previous_plugin(path, record, execute)
            elif _find_installed_plugin(listed) is not None:
                plugin_remove = _set_mutation(
                    path,
                    record,
                    "plugin_remove",
                    command=[codex_bin, "plugin", "remove", SELECTOR, "--json"],
                )
                _invoke(execute, plugin_remove["command"])
                plugin_remove["state"] = "APPLIED"
                plugin_remove["completed_at"] = utc_now()
            else:
                plugin_remove = _mutation(record, "plugin_remove")
                if plugin_remove is not None and plugin_remove.get("state") == "INTENT":
                    plugin_remove["state"] = "APPLIED"
                    plugin_remove["completed_at"] = utc_now()
                    plugin_remove["reconciled_from"] = "plugin-list"
            plugin_add["state"] = "ROLLED_BACK"
            plugin_add["rolled_back_at"] = utc_now()
            _write_record(path, record)

        marketplace_add = _mutation(record, "marketplace_add")
        if (
            marketplace_add is not None
            and marketplace_add.get("state") in {"INTENT", "APPLIED"}
            and record["previous"].get("marketplace") is None
        ):
            marketplaces = _json_output(
                _invoke(
                    execute,
                    [codex_bin, "plugin", "marketplace", "list", "--json"],
                )
            )
            if _find_marketplace(marketplaces) is not None:
                marketplace_remove = _set_mutation(
                    path,
                    record,
                    "marketplace_remove",
                    command=[
                        codex_bin,
                        "plugin",
                        "marketplace",
                        "remove",
                        MARKETPLACE_NAME,
                    ],
                )
                _invoke(execute, marketplace_remove["command"])
                marketplace_remove["state"] = "APPLIED"
                marketplace_remove["completed_at"] = utc_now()
            else:
                marketplace_remove = _mutation(record, "marketplace_remove")
                if (
                    marketplace_remove is not None
                    and marketplace_remove.get("state") == "INTENT"
                ):
                    marketplace_remove["state"] = "APPLIED"
                    marketplace_remove["completed_at"] = utc_now()
                    marketplace_remove["reconciled_from"] = "marketplace-list"
            marketplace_add["state"] = "ROLLED_BACK"
            marketplace_add["rolled_back_at"] = utc_now()
            _write_record(path, record)
    except Exception as error:
        record["status"] = "ROLLBACK_FAILED"
        record["rollback_failure"] = {
            "type": type(error).__name__,
            "message": str(error),
        }
        _write_record(path, record)
        raise
    record["status"] = "ROLLED_BACK"
    record["rolled_back_at"] = utc_now()
    record["restart_required"] = True
    _write_record(path, record)
    return deepcopy(record)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="install_plugin.py")
    commands = root.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("preinstall")
    prepare.add_argument("--plugin-root", type=Path, required=True)
    prepare.add_argument("--codex-home", type=Path, required=True)
    prepare.add_argument("--codex-bin", default="codex")
    prepare.add_argument("--record", type=Path, required=True)
    for name in ("activate", "rollback"):
        command = commands.add_parser(name)
        command.add_argument("--record", type=Path, required=True)
    return root


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = parser().parse_args(argv)
        if arguments.command == "preinstall":
            result = preinstall(
                arguments.plugin_root,
                arguments.codex_home,
                arguments.codex_bin,
                record_path=arguments.record,
            )
        elif arguments.command == "activate":
            result = activate(arguments.record)
        else:
            result = rollback(arguments.record)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception as error:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": type(error).__name__,
                    "message": str(error),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
