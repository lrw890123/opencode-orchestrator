#!/usr/bin/env python3
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import uuid


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from opencode_orchestrator.git_workspace import GitWorkspace
from opencode_orchestrator.opencode_client import OpenCodeClient
from opencode_orchestrator.task_state import atomic_write_json
from tests.support.mcp_client import MCPSubprocessClient


MCP_SERVER = ROOT / "mcp/server.py"
REVIEW_SUMMARY = "Codex inspected every changed file and reran the contract tests."


def run(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )


def create_source_repo(state_root: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    source = state_root / "e2e-sources" / f"{stamp}-{uuid.uuid4().hex[:8]}"
    (source / "tests").mkdir(parents=True)
    (source / "math_utils.py").write_text(
        "def add(a: int, b: int) -> int:\n"
        "    return a + b\n",
        encoding="utf-8",
    )
    (source / "tests/test_math_utils.py").write_text(
        "import unittest\n"
        "from math_utils import add\n\n\n"
        "class MathUtilsTest(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n",
        encoding="utf-8",
    )
    (source / ".gitignore").write_text("__pycache__/\n*.py[cod]\n", encoding="utf-8")
    run(["git", "init", "-b", "main", str(source)])
    run(["git", "config", "user.email", "e2e@example.com"], cwd=source)
    run(["git", "config", "user.name", "OpenCode Plugin E2E"], cwd=source)
    run(["git", "add", ".gitignore", "math_utils.py", "tests/test_math_utils.py"], cwd=source)
    run(["git", "commit", "-m", "Create Plugin E2E fixture"], cwd=source)
    return source


def parse_model(value: str) -> dict[str, str]:
    if "/" not in value:
        raise ValueError("model must use provider/model format")
    provider_id, model_id = value.split("/", 1)
    if not provider_id or not model_id:
        raise ValueError("model must use provider/model format")
    return {"providerID": provider_id, "modelID": model_id}


def task_contract(cross_worktree_read_root: Path | None = None) -> dict:
    if cross_worktree_read_root is None:
        goal = "Add a typed multiply(a, b) function and a focused unittest for 6 × 7 = 42."
        approved_plan = [
            "Inspect math_utils.py and tests/test_math_utils.py",
            "Add multiply with direct multiplication",
            "Add one focused unittest and run the full unittest command",
        ]
        acceptance = [
            "multiply(6, 7) returns 42",
            "Existing add test and new multiply test pass",
        ]
    else:
        fixture_a = cross_worktree_read_root / "fixture-a.txt"
        fixture_b = cross_worktree_read_root / "fixture-b.txt"
        goal = (
            "Read the two known fixture files "
            f"{fixture_a} and {fixture_b} in parallel, then add a typed "
            "multiply(a, b) function and a focused unittest for 6 × 7 = 42."
        )
        approved_plan = [
            "Read fixture-a.txt and fixture-b.txt from the approved external directory in parallel",
            "Inspect math_utils.py and tests/test_math_utils.py",
            "Add multiply with direct multiplication",
            "Add one focused unittest and run the full unittest command",
        ]
        acceptance = [
            "Both external fixture reads complete without changing the fixtures",
            "multiply(6, 7) returns 42",
            "Existing add test and new multiply test pass",
        ]
    return {
        "goal": goal,
        "non_goals": ["Do not change add()", "Do not add dependencies"],
        "approved_plan": approved_plan,
        "allowed_paths": ["math_utils.py", "tests/test_math_utils.py"],
        "forbidden_actions": [
            "Do not modify other files",
            "Do not commit, push, publish, delete, or change dependencies",
        ],
        "acceptance_criteria": acceptance,
        "test_commands": ["python3 -m unittest discover -s tests -v"],
        "risk": {
            "file_count": 2,
            "line_count": 20,
            "cross_module": False,
            "public_interface": False,
            "dependency_change": False,
            "high_risk_actions": [],
        },
        "user_approved": False,
    }


def delegate_arguments(
    source: Path,
    server: str,
    model: str,
    effort: str,
    cross_worktree_read_root: Path | None = None,
) -> dict:
    arguments = {
        "repo_path": str(source),
        "task_contract": task_contract(cross_worktree_read_root),
        "model": parse_model(model),
        "effort": effort,
        "timeout_seconds": 600,
        "server_url": server,
        "slug": "multiply-demo",
    }
    if cross_worktree_read_root is not None:
        arguments["permission_policy"] = {
            "rules": [
                {
                    "permission": "external_directory",
                    "pattern": f"{cross_worktree_read_root}/**",
                    "action": "allow",
                }
            ]
        }
    return arguments


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _persisted_task_directory_ids(state_root: Path) -> set[str]:
    tasks_root = state_root / "tasks"
    if not tasks_root.is_dir():
        return set()
    return {path.name for path in tasks_root.iterdir() if path.is_dir()}


def _persisted_run_evidence(
    state_root: Path,
    prior_task_ids: set[str],
) -> tuple[set[str], set[str]]:
    task_ids: set[str] = set()
    session_ids: set[str] = set()
    tasks_root = state_root / "tasks"
    if not tasks_root.is_dir():
        return task_ids, session_ids
    for task_dir in sorted(path for path in tasks_root.iterdir() if path.is_dir()):
        if task_dir.name in prior_task_ids:
            continue
        state_path = task_dir / "state.json"
        require(state_path.is_file(), f"new task has no persisted state: {task_dir.name}")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        task_id = state.get("task_id") if isinstance(state, dict) else None
        require(task_id == task_dir.name, f"persisted task directory mismatch: {task_dir.name}")
        opencode = state.get("opencode")
        session_id = opencode.get("session_id") if isinstance(opencode, dict) else None
        require(
            isinstance(session_id, str) and bool(session_id.strip()),
            f"persisted task has no session evidence: {task_dir.name}",
        )
        task_ids.add(task_id)
        session_ids.add(session_id)
    return task_ids, session_ids


def _owned_run_task_id(
    state_root: Path,
    prior_task_ids: set[str],
    source: Path,
) -> str | None:
    tasks_root = state_root / "tasks"
    if not tasks_root.is_dir():
        return None
    matches: list[str] = []
    source_path = str(source.resolve())
    for task_dir in sorted(path for path in tasks_root.iterdir() if path.is_dir()):
        if task_dir.name in prior_task_ids:
            continue
        state_path = task_dir / "state.json"
        if not state_path.is_file():
            continue
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(state, dict):
            continue
        persisted_source = state.get("source")
        repo_root = (
            persisted_source.get("repo_root")
            if isinstance(persisted_source, dict)
            else None
        )
        if state.get("task_id") == task_dir.name and repo_root == source_path:
            matches.append(task_dir.name)
    return matches[0] if len(matches) == 1 else None


def tool_call(
    client: MCPSubprocessClient,
    request_id: int,
    name: str,
    arguments: dict,
) -> dict:
    response = client.request(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        timeout=660,
    )
    if "error" in response:
        raise RuntimeError(f"MCP {name} protocol error: {response['error']}")
    result = response["result"]
    if result.get("isError"):
        raise RuntimeError(f"MCP {name} tool error: {result['content']}")
    return result["structuredContent"]


def _last_assistant_text(transcript: list[dict]) -> str:
    for message in reversed(transcript):
        if message.get("role") != "assistant":
            continue
        texts = [part["text"] for part in message.get("parts", []) if part.get("type") == "text"]
        if texts:
            return "\n".join(texts)
    return ""


def execute(
    server: str,
    state_root: Path,
    dry_run: bool,
    model: str,
    effort: str,
    cross_worktree_read_root: Path | None = None,
) -> dict:
    state_root = state_root.expanduser().resolve()
    state_root.mkdir(parents=True, exist_ok=True)
    prior_task_ids = _persisted_task_directory_ids(state_root)
    if cross_worktree_read_root is not None:
        if not cross_worktree_read_root.is_absolute():
            raise ValueError("cross-worktree-read-root must be absolute")
        cross_worktree_read_root = cross_worktree_read_root.resolve()
    source = create_source_repo(state_root)
    arguments = delegate_arguments(
        source,
        server,
        model,
        effort,
        cross_worktree_read_root,
    )
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "source_repo": str(source),
            "delegate_arguments": arguments,
        }

    source_before = GitWorkspace(source).dirty_fingerprint()
    source_head_before = run(["git", "rev-parse", "HEAD"], cwd=source).stdout.strip()
    client = MCPSubprocessClient(MCP_SERVER, state_root=state_root)
    mcp_call_count = 0
    delegate_call_count = 0
    run_completed = False
    try:
        initialized = client.request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "live-plugin-e2e", "version": "1"},
                },
            }
        )
        require(
            initialized["result"]["protocolVersion"] == "2025-06-18",
            "MCP protocol negotiation failed",
        )
        client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        delegate_call_count += 1
        mcp_call_count += 1
        delegated = tool_call(client, 10, "delegate_and_wait", arguments)
        require(delegated["outcome"] == "COMPLETED", f"delegate outcome: {delegated}")
        task_id = delegated["task_id"]
        state_path = state_root / "tasks" / task_id / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        worktree = Path(state["worktree"]["path"])
        session_id = state["opencode"]["session_id"]

        mcp_call_count += 1
        first_collection = tool_call(
            client,
            11,
            "collect_result",
            {"task_id": task_id},
        )
        first = first_collection["artifacts"]["result"]
        require(not first["out_of_scope"], f"out-of-scope changes: {first['out_of_scope']}")
        require(
            first["changed_files"] == ["math_utils.py", "tests/test_math_utils.py"],
            f"unexpected changed files: {first['changed_files']}",
        )
        tests = run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            cwd=worktree,
        )
        require("multiply" in (worktree / "math_utils.py").read_text(), "multiply is missing")
        require(
            "test_multiply" in (worktree / "tests/test_math_utils.py").read_text(),
            "multiply test is missing",
        )

        diff_before_review = GitWorkspace(worktree).dirty_fingerprint()
        mcp_call_count += 1
        reviewed = tool_call(
            client,
            12,
            "reply_and_wait",
            {
                "task_id": task_id,
                "kind": "review",
                "payload": {
                    "text": (
                        "Codex review passed. Perform a read-only recheck of the changed files and "
                        "test result. Do not modify files. Reply with OPENCODE_REVIEW_ACK and the "
                        "test command observed."
                    )
                },
                "timeout_seconds": 600,
            },
        )
        require(reviewed["outcome"] == "COMPLETED", f"review outcome: {reviewed}")

        mcp_call_count += 1
        final_collection = tool_call(
            client,
            13,
            "collect_result",
            {
                "task_id": task_id,
                "review_evidence": {
                    "tests_passed": True,
                    "review_summary": REVIEW_SUMMARY,
                },
            },
        )
        final_result = final_collection["artifacts"]["result"]
        require(
            final_collection["review_state"] == "AWAITING_INTEGRATION",
            f"final review state: {final_collection['review_state']}",
        )
        require(
            GitWorkspace(worktree).dirty_fingerprint() == diff_before_review,
            "read-only review changed the worktree",
        )

        mcp_call_count += 1
        transcript_result = tool_call(
            client,
            14,
            "read_transcript",
            {
                "task_id": task_id,
                "limit": 100,
                "include_tool_output": False,
            },
        )
        transcript = transcript_result["artifacts"]["transcript"]["messages"]
        prompt_count = sum(message.get("role") == "user" for message in transcript)
        review_ack = _last_assistant_text(transcript)
        require("OPENCODE_REVIEW_ACK" in review_ack, "review acknowledgement missing")

        source_after = GitWorkspace(source).dirty_fingerprint()
        source_head_after = run(["git", "rev-parse", "HEAD"], cwd=source).stdout.strip()
        merged = source_head_after != source_head_before
        pushed = bool(run(["git", "remote"], cwd=source).stdout.strip())
        cleaned = not worktree.is_dir()
        require(delegate_call_count == 1, "unexpected model-side polling")
        require(prompt_count == 2, "expected initial and review prompts")
        require(effort == "max", "effort changed")
        require(
            not merged and not pushed and not cleaned,
            "delivery boundary violated",
        )
        require(source_before == source_after, "source repository state changed")

        session = OpenCodeClient(server, worktree).session(session_id)
        observed_task_ids, observed_session_ids = _persisted_run_evidence(
            state_root,
            prior_task_ids,
        )
        require(observed_task_ids == {task_id}, "expected one persisted task for this run")
        require(observed_session_ids == {session_id}, "expected one persisted session for this run")
        expected_model = {
            "id": arguments["model"]["modelID"],
            "providerID": arguments["model"]["providerID"],
            "variant": effort,
        }
        require(session.get("model") == expected_model, "OpenCode model/effort changed")
        report = {
            "ok": True,
            "dry_run": False,
            "server": server,
            "server_version": state["opencode"].get("version"),
            "requested_model": arguments["model"],
            "effort": effort,
            "observed_session_model": session.get("model"),
            "task_id": task_id,
            "session_id": session_id,
            "session_directory": session["directory"],
            "source_repo": str(source),
            "worktree": str(worktree),
            "branch": state["worktree"]["branch"],
            "base_sha": state["source"]["base_sha"],
            "delegate_call_count": delegate_call_count,
            "mcp_call_count": mcp_call_count,
            "prompt_count": prompt_count,
            "changed_files": first["changed_files"],
            "untracked_files": first["untracked_files"],
            "diff": {
                "stat": first["diff_stat"],
                "opencode": first["opencode_diff"],
            },
            "test_command": f"{sys.executable} -m unittest discover -s tests -v",
            "test_output": tests.stderr + tests.stdout,
            "poll_fallback_used": first["poll_fallback_used"],
            "transcript": transcript,
            "review_ack": review_ack,
            "final_review_state": final_collection["review_state"],
            "final_result": final_result,
            "source_fingerprint_before": source_before,
            "source_fingerprint_after": source_after,
            "merged": merged,
            "pushed": pushed,
            "cleaned": cleaned,
            "cross_worktree_read_root": (
                str(cross_worktree_read_root) if cross_worktree_read_root else None
            ),
            "permission_replies": deepcopy(state.get("permission_audit") or []),
            "pending_input_counts": {
                "permissions": len((state.get("progress") or {}).get("pending_permissions") or []),
                "questions": len((state.get("progress") or {}).get("pending_questions") or []),
            },
            "last_meaningful_progress": (state.get("progress") or {}).get("last_progress_at"),
            "idle_duration_seconds": (state.get("progress") or {}).get("idle_seconds"),
            "task_count": len(observed_task_ids),
            "session_count": len(observed_session_ids),
        }
        report_path = state_root / f"live-plugin-e2e-{task_id}.json"
        report["report_path"] = str(report_path)
        atomic_write_json(report_path, report)
        run_completed = True
        return report
    finally:
        if not run_completed:
            owned_task_id = _owned_run_task_id(state_root, prior_task_ids, source)
            if owned_task_id is not None:
                try:
                    cleanup = tool_call(
                        client,
                        999,
                        "abort_task",
                        {"task_id": owned_task_id},
                    )
                    require(
                        cleanup.get("outcome") == "ABORTED",
                        f"cleanup abort outcome: {cleanup}",
                    )
                except Exception as cleanup_error:
                    print(
                        f"live_plugin_e2e cleanup failed for {owned_task_id}: {cleanup_error}",
                        file=sys.stderr,
                    )
        _, stderr = client.close()
        if stderr:
            print(stderr, file=sys.stderr, end="")


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="http://127.0.0.1:4096")
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--model", default="mcli/glm-5.3")
    parser.add_argument("--effort", default="max")
    parser.add_argument("--cross-worktree-read-root", type=Path)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    try:
        arguments = parse_args(argv)
        result = execute(
            arguments.server,
            arguments.state_root,
            arguments.dry_run,
            arguments.model,
            arguments.effort,
            arguments.cross_worktree_read_root,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as error:
        print(f"live_plugin_e2e: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
