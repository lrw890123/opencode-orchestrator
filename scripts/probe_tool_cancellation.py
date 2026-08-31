#!/usr/bin/env python3
"""Run a bounded, disposable experiment for OpenCode tool-part cancellation.

This module deliberately does not add a production cancellation API.  It creates
one temporary Git repository, asks one disposable OpenCode session to launch
two quick commands and one long-running Python command, and records whether
deleting only the long-running message part has the expected effects.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable, Mapping
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from opencode_orchestrator.opencode_client import OpenCodeClient


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
GATE_NAMES = (
    "tool_stopped",
    "model_resumed",
    "parallel_calls_valid",
    "transcript_consistent",
    "idempotent",
)


def parse_model(value: str) -> dict[str, str]:
    """Parse the CLI's provider/model spelling without silently guessing."""

    if not isinstance(value, str) or value.count("/") != 1:
        raise ValueError("model must use provider/model format")
    provider_id, model_id = value.split("/", 1)
    if not provider_id.strip() or not model_id.strip():
        raise ValueError("model must use provider/model format")
    return {"providerID": provider_id, "modelID": model_id}


def validate_loopback_server(server: str) -> None:
    parsed = urlparse(server)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"invalid OpenCode server URL: {server}")
    if parsed.hostname not in LOOPBACK_HOSTS:
        raise ValueError(f"non-loopback OpenCode server requires explicit override: {server}")


def _run(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=cwd, check=True, text=True, capture_output=True)


def create_temporary_git_repo(parent: Path) -> Path:
    """Create and commit a minimal repository below a TemporaryDirectory."""

    repo = parent / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("cancellation probe fixture\n", encoding="utf-8")
    _run(["git", "init", "-b", "main", str(repo)])
    _run(["git", "config", "user.email", "probe@example.invalid"], cwd=repo)
    _run(["git", "config", "user.name", "OpenCode cancellation probe"], cwd=repo)
    _run(["git", "add", "README.md"], cwd=repo)
    _run(["git", "commit", "-m", "Create cancellation probe fixture"], cwd=repo)
    return repo


def cancellation_prompt() -> str:
    """Return the single prompt used by both disposable runs."""

    return (
        "This is a disposable OpenCode tool cancellation experiment. In this Git "
        "repository, execute exactly three independent operations using three "
        "separate shell tool calls. Start all three operations without combining "
        "them into one shell command, and keep the session active while the long "
        "operation runs. First write exactly `quick-a\\n` to a file named `quick-a` "
        "with `printf 'quick-a\\n' > quick-a`. Second run this Python process: "
        "`python3 -c 'import os,pathlib,time; pathlib.Path(\"sleeper.pid\").write_text("
        "str(os.getpid())); time.sleep(120); pathlib.Path(\"sleeper-done\").write_text("
        "\"sleeper-done\")'`; it must write its PID to `sleeper.pid`, sleep for "
        "120 seconds, and only then write `sleeper-done`. Third write exactly "
        "`quick-c\\n` to a file named `quick-c` with `printf 'quick-c\\n' > quick-c`. "
        "Do not wait for the sleeper to finish before continuing the independent "
        "quick operation."
    )


def _first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
    return None


def _message_id(message: Mapping[str, Any]) -> str | None:
    info = message.get("info")
    return _first_string(
        info.get("id") if isinstance(info, Mapping) else None,
        message.get("id"),
        message.get("messageID"),
    )


def _part_id(part: Mapping[str, Any]) -> str | None:
    return _first_string(part.get("id"), part.get("partID"), part.get("partId"))


def _call_id(part: Mapping[str, Any]) -> str | None:
    return _first_string(part.get("callID"), part.get("callId"), part.get("call_id"))


def _part_status(part: Mapping[str, Any]) -> str | None:
    state = part.get("state")
    if isinstance(state, Mapping):
        status = state.get("status")
        if isinstance(status, str):
            return status.lower()
    status = part.get("status")
    return status.lower() if isinstance(status, str) else None


def _string_values(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for nested in value.values():
            yield from _string_values(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _string_values(nested)


def _part_command(part: Mapping[str, Any]) -> str:
    state = part.get("state")
    candidates = [part.get("command"), part.get("input"), part.get("cmd")]
    if isinstance(state, Mapping):
        candidates.extend([state.get("command"), state.get("input"), state.get("cmd")])
    return "\n".join(_string_values(candidates))


def _is_sleeper_command(command: str) -> bool:
    lowered = command.lower()
    return "sleeper.pid" in lowered and (
        "sleep(120" in lowered or "sleep (120" in lowered or "sleep 120" in lowered
    )


def find_running_sleeper_part(messages: Any) -> dict[str, str] | None:
    """Find a running sleeper by its message, part, and tool-call IDs."""

    if not isinstance(messages, list):
        return None
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        message_id = _message_id(message)
        parts = message.get("parts")
        if not message_id or not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, Mapping) or part.get("type") != "tool":
                continue
            if _part_status(part) not in {"running", "started", "executing", "pending"}:
                continue
            if not _is_sleeper_command(_part_command(part)):
                continue
            part_id = _part_id(part)
            call_id = _call_id(part)
            if part_id and call_id:
                return {
                    "message_id": message_id,
                    "part_id": part_id,
                    "call_id": call_id,
                }
    return None


def _find_part(messages: Any, target: Mapping[str, str]) -> Mapping[str, Any] | None:
    if not isinstance(messages, list):
        return None
    for message in messages:
        if not isinstance(message, Mapping) or _message_id(message) != target.get("message_id"):
            continue
        parts = message.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if isinstance(part, Mapping) and _part_id(part) == target.get("part_id"):
                return part
    return None


def _tool_call_ids(messages: Any, marker: str) -> list[str]:
    ids = []
    if not isinstance(messages, list):
        return ids
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        for part in message.get("parts") or []:
            if not isinstance(part, Mapping) or part.get("type") != "tool":
                continue
            if marker in _part_command(part):
                call_id = _call_id(part)
                if call_id:
                    ids.append(call_id)
    return ids


def transcript_consistent(messages: Any) -> bool:
    """Check that an OpenCode message response remains structurally parseable."""

    if not isinstance(messages, list):
        return False
    try:
        json.dumps(messages, ensure_ascii=False)
    except (TypeError, ValueError):
        return False
    for message in messages:
        if not isinstance(message, Mapping):
            return False
        parts = message.get("parts")
        if not isinstance(parts, list):
            return False
        if "info" in message and not isinstance(message.get("info"), Mapping):
            return False
        for part in parts:
            if not isinstance(part, Mapping):
                return False
            if "type" in part and not isinstance(part.get("type"), str):
                return False
            if "state" in part and not isinstance(part.get("state"), Mapping):
                return False
    return True


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeError):
        return None


def _quick_results_valid(repo: Path, messages: Any) -> bool:
    if _read_text(repo / "quick-a") != "quick-a\n":
        return False
    if _read_text(repo / "quick-c") != "quick-c\n":
        return False
    quick_a = set(_tool_call_ids(messages, "quick-a"))
    quick_c = set(_tool_call_ids(messages, "quick-c"))
    return bool(quick_a and quick_c and quick_a.isdisjoint(quick_c))


def pid_alive(pid: int) -> bool:
    """Return false for exited processes, including zombies where ``ps`` reports it."""

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    try:
        status = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            check=False,
            text=True,
            capture_output=True,
        ).stdout.strip()
    except OSError:
        return True
    return bool(status) and not status.startswith("Z")


def sleeper_pid(repo: Path) -> int | None:
    value = _read_text(repo / "sleeper.pid")
    if value is None:
        return None
    try:
        pid = int(value.strip())
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def delete_tool_part(
    client: Any,
    session_id: str,
    message_id: str,
    part_id: str,
    *,
    urlopen: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    """DELETE exactly one OpenCode message part; classify replay as harmless."""

    path = f"/session/{session_id}/message/{message_id}/part/{part_id}"
    if hasattr(client, "_url"):
        request_url = client._url(path, scoped=True)
    else:
        base_url = str(getattr(client, "base_url")).rstrip("/")
        directory = str(getattr(client, "directory"))
        request_url = f"{base_url}{path}?{urlencode({'directory': directory})}"
    headers = client._headers() if hasattr(client, "_headers") else {"Accept": "application/json"}
    request = Request(request_url, headers=headers, method="DELETE")
    timeout = float(getattr(client, "timeout", 10.0))
    try:
        with urlopen(request, timeout=timeout) as response:
            response.read()
            status = int(getattr(response, "status", 200))
            return {
                "accepted": 200 <= status < 300,
                "status": status,
                "already_absent": False,
            }
    except HTTPError as error:
        try:
            error.read()
        except Exception:
            pass
        try:
            if error.code in {404, 409}:
                return {"accepted": False, "status": error.code, "already_absent": True}
        finally:
            error.close()
        raise


def all_gate_booleans(evidence: Mapping[str, Any]) -> bool:
    return all(evidence.get(name) is True for name in GATE_NAMES)


def production_supported(evidence: Mapping[str, Any]) -> bool:
    """The single-run probe can never enable production support."""

    # Even an all-true disposable run is only one half of the required
    # consecutive-run gate.  The runtime remains unsupported in this lane.
    return False


def _safe_failure(
    error: BaseException,
    repo: Path | None = None,
    extra_paths: tuple[Path, ...] = (),
) -> dict[str, str]:
    message = str(error)
    replacements = [str(ROOT), str(Path.cwd().resolve())]
    if repo is not None:
        replacements.insert(0, str(repo))
    replacements.extend(str(path.resolve()) for path in extra_paths)
    for path in replacements:
        if path:
            message = message.replace(path, "<local-path>")
    return {"type": type(error).__name__, "message": message[:1000]}


def _redact_paths(value: Any, paths: tuple[Path, ...]) -> Any:
    if isinstance(value, str):
        result = value
        for path in paths:
            result = result.replace(str(path.resolve()), "<local-path>")
        return result
    if isinstance(value, dict):
        return {key: _redact_paths(item, paths) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_paths(item, paths) for item in value]
    return value


def _assistant_texts(messages: Any) -> list[str]:
    texts = []
    if not isinstance(messages, list):
        return texts
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        info = message.get("info")
        if isinstance(info, Mapping) and info.get("role") != "assistant":
            continue
        for part in message.get("parts") or []:
            if isinstance(part, Mapping) and part.get("type") == "text" and part.get("text"):
                texts.append(str(part["text"]))
    return texts


def _has_advanced(
    before: Any,
    after: Any,
    target: Mapping[str, str],
) -> bool:
    before_ids = {
        _message_id(item)
        for item in before
        if isinstance(item, Mapping) and _message_id(item)
    } if isinstance(before, list) else set()
    after_ids = {
        _message_id(item)
        for item in after
        if isinstance(item, Mapping) and _message_id(item)
    } if isinstance(after, list) else set()
    if after_ids - before_ids:
        return True
    before_part = _find_part(before, target)
    after_part = _find_part(after, target)
    before_status = _part_status(before_part) if before_part else None
    after_status = _part_status(after_part) if after_part else None
    if before_status in {"running", "started", "executing", "pending"} and after_status not in {
        None,
        "running",
        "started",
        "executing",
        "pending",
    }:
        return True
    return len(_assistant_texts(after)) > len(_assistant_texts(before))


def _observe(
    client: Any,
    session_id: str,
    repo: Path,
    target: Mapping[str, str],
    before: Any,
    *,
    timeout: float,
    poll_interval: float,
    sleeper: Callable[[float], None],
    is_pid_alive: Callable[[int], bool],
) -> tuple[dict[str, Any], Any]:
    deadline = time.monotonic() + timeout
    latest = before
    model_resumed = False
    tool_stopped = False
    parallel_calls_valid = False
    while True:
        latest = client.messages(session_id, limit=100)
        model_resumed = model_resumed or _has_advanced(before, latest, target)
        parallel_calls_valid = _quick_results_valid(repo, latest)
        pid = sleeper_pid(repo)
        part = _find_part(latest, target)
        current_status = _part_status(part) if part else None
        tool_stopped = (
            pid is not None
            and not is_pid_alive(pid)
            and current_status not in {"running", "started", "executing", "pending"}
        )
        if tool_stopped and model_resumed and parallel_calls_valid:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        sleeper(min(poll_interval, remaining))
    return {
        "tool_stopped": tool_stopped,
        "model_resumed": model_resumed,
        "parallel_calls_valid": parallel_calls_valid,
        "transcript_consistent": transcript_consistent(latest),
    }, latest


def _default_client_factory(server: str, directory: Path) -> OpenCodeClient:
    return OpenCodeClient(
        server,
        directory,
        username=os.environ.get("OPENCODE_SERVER_USERNAME"),
        password=os.environ.get("OPENCODE_SERVER_PASSWORD"),
        timeout=3.0,
    )


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_probe(
    server: str,
    model: str,
    effort: str,
    output: Path,
    *,
    client_factory: Callable[[str, Path], Any] = _default_client_factory,
    poll_timeout: float = 30.0,
    observe_timeout: float = 15.0,
    poll_interval: float = 0.5,
    sleeper: Callable[[float], None] = time.sleep,
    is_pid_alive: Callable[[int], bool] = pid_alive,
    production_root: Path = ROOT,
    urlopen: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    """Run one bounded experiment and always write its evidence JSON."""

    validate_loopback_server(server)
    selected_model = parse_model(model)
    if not isinstance(effort, str) or not effort.strip():
        raise ValueError("effort must be non-empty")
    if poll_timeout < 0 or observe_timeout < 0 or poll_interval < 0:
        raise ValueError("probe timeouts and poll interval must be non-negative")

    output = Path(output)
    started_at = datetime.now(timezone.utc).isoformat()
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "started_at": started_at,
        "server": server,
        "requested_model": selected_model,
        "effort": effort,
        "opencode_version": None,
        "session_id": None,
        "target": None,
        "tool_stopped": False,
        "model_resumed": False,
        "parallel_calls_valid": False,
        "transcript_consistent": False,
        "idempotent": False,
        "production_supported": False,
        "cleanup": {
            "temporary_repo_removed": False,
            "session_abort_called": False,
            "session_abort_ok": False,
            "managed_state_touched": False,
        },
        "conclusion": "unsupported in 2.1.0",
    }
    client = None
    session_id: str | None = None
    repo: Path | None = None
    temporary_repo: Path | None = None

    try:
        # TemporaryDirectory is intentionally the only lifecycle owner.  No
        # managed task/worktree directory is used by this experiment.
        with TemporaryDirectory(prefix="opencode-tool-cancel-") as temporary:
            temporary_repo = Path(temporary) / "repo"
            try:
                repo = create_temporary_git_repo(Path(temporary))
                client = client_factory(server, repo)
                health = client.health()
                if isinstance(health, Mapping):
                    evidence["opencode_version"] = health.get("version")
                client.validate_model_selection(
                    selected_model["providerID"], selected_model["modelID"], effort
                )
                session = client.create_session("Tool cancellation feasibility probe")
                if not isinstance(session, Mapping) or not isinstance(session.get("id"), str):
                    raise RuntimeError("OpenCode returned a session without an id")
                session_id = session["id"]
                evidence["session_id"] = session_id
                client.prompt_async(
                    session_id,
                    cancellation_prompt(),
                    model=selected_model,
                    variant=effort,
                )

                deadline = time.monotonic() + poll_timeout
                target = None
                before = []
                while time.monotonic() <= deadline:
                    before = client.messages(session_id, limit=100)
                    target = find_running_sleeper_part(before)
                    if target is not None:
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    sleeper(min(poll_interval, remaining))
                if target is None:
                    raise TimeoutError("running sleeper tool part was not observed within 30 seconds")
                evidence["target"] = target

                first_delete = delete_tool_part(
                    client,
                    session_id,
                    target["message_id"],
                    target["part_id"],
                    urlopen=urlopen,
                )
                evidence["delete"] = {
                    "first_status": first_delete.get("status"),
                    "first_accepted": first_delete.get("accepted") is True,
                }
                observation, _latest = _observe(
                    client,
                    session_id,
                    repo,
                    target,
                    before,
                    timeout=observe_timeout,
                    poll_interval=poll_interval,
                    sleeper=sleeper,
                    is_pid_alive=is_pid_alive,
                )
                evidence.update(observation)
                second_delete = delete_tool_part(
                    client,
                    session_id,
                    target["message_id"],
                    target["part_id"],
                    urlopen=urlopen,
                )
                evidence["delete"].update(
                    {
                        "second_status": second_delete.get("status"),
                        "second_accepted": second_delete.get("accepted") is True,
                    }
                )
                evidence["idempotent"] = bool(
                    first_delete.get("accepted") is True
                    and (
                        second_delete.get("accepted") is True
                        or second_delete.get("already_absent") is True
                    )
                )
            except BaseException as error:
                evidence["failure"] = _safe_failure(error, repo, (Path(production_root),))
            finally:
                if client is not None and session_id is not None:
                    evidence["cleanup"]["session_abort_called"] = True
                    try:
                        evidence["cleanup"]["session_abort_ok"] = bool(client.abort(session_id))
                    except BaseException as error:
                        evidence["cleanup"]["abort_failure"] = _safe_failure(
                            error, repo, (Path(production_root),)
                        )
            # The TemporaryDirectory has not exited yet, so defer this check
            # until after the context manager below has removed the repository.
        evidence["cleanup"]["temporary_repo_removed"] = bool(
            temporary_repo is not None and not temporary_repo.exists()
        )
    except BaseException as error:
        # This catches fixture setup and TemporaryDirectory failures as well;
        # output still contains a machine-readable, conservative result.
        evidence.setdefault(
            "failure", _safe_failure(error, repo, (Path(production_root),))
        )
        evidence["cleanup"]["temporary_repo_removed"] = bool(
            temporary_repo is not None and not temporary_repo.exists()
        )

    evidence["production_supported"] = production_supported(evidence)
    evidence["gate_results"] = {name: evidence.get(name) is True for name in GATE_NAMES}
    evidence["conclusion"] = (
        "unsupported in 2.1.0"
        if not all_gate_booleans(evidence)
        else "all gates passed once; unsupported in 2.1.0 until a second consecutive run"
    )
    evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
    evidence["cleanup"]["managed_state_touched"] = False
    # Avoid recording any caller-supplied production worktree path.  The only
    # local path touched is the disposable TemporaryDirectory above.
    redaction_paths = (Path(production_root), ROOT, Path.cwd().resolve())
    evidence = _redact_paths(evidence, redaction_paths)
    _write_json(output, evidence)
    return evidence


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="http://127.0.0.1:4096")
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = parse_args(argv)
        evidence = run_probe(
            arguments.server,
            arguments.model,
            arguments.effort,
            arguments.output,
        )
        print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as error:
        print(f"probe_tool_cancellation: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
