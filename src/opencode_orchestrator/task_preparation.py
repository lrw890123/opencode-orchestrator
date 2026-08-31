from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from .contracts import TASK_CONTRACT_KEYS
from .git_workspace import GitWorkspace
from .permission_policy import normalize_permission_policy, normalize_progress_policy
from .policy import classify_risk
from .task_state import ExecutionState, Phase, TaskStore, atomic_write_json, new_task_id


class TaskPreparer:
    """Validate contracts and create isolated task worktrees."""

    def __init__(self, state_root: Path, store: TaskStore):
        self.state_root = state_root
        self.store = store

    @staticmethod
    def _validate_request(request: dict) -> None:
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        missing = sorted(TASK_CONTRACT_KEYS - set(request))
        if missing:
            raise ValueError(f"request is missing required keys: {', '.join(missing)}")
        if not request["goal"] or not request["allowed_paths"]:
            raise ValueError("request goal and allowed_paths must be non-empty")
        model = request.get("model")
        if model is not None:
            if not isinstance(model, dict) or set(model) != {"providerID", "modelID"}:
                raise ValueError("request model must contain exactly providerID and modelID")
            if not all(
                isinstance(model[key], str) and model[key].strip() for key in model
            ):
                raise ValueError(
                    "request model providerID and modelID must be non-empty strings"
                )
        effort = request.get("effort")
        if not isinstance(effort, str) or not effort.strip():
            raise ValueError("request effort must be a non-empty string")
        request["permission_policy"] = normalize_permission_policy(
            request.get("permission_policy")
        )
        request["progress_policy"] = normalize_progress_policy(
            request.get("progress_policy")
        )

    @staticmethod
    def task_fingerprint(base_sha: str, request: dict) -> str:
        canonical = json.dumps(
            {"base_sha": base_sha, "request": request},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(canonical).hexdigest()}"

    def prepare_task(
        self,
        repo: Path,
        slug: str,
        request: dict,
        server_url: str = "http://127.0.0.1:4096",
    ) -> dict:
        request = deepcopy(request)
        request.setdefault("effort", "max")
        self._validate_request(request)
        workspace = GitWorkspace(repo)
        facts = workspace.facts()
        fingerprint = self.task_fingerprint(facts.head_sha, request)
        task_id = new_task_id()
        self.store.create(
            task_id,
            str(facts.repo_root),
            facts.head_sha,
            facts.branch,
            facts.dirty_fingerprint,
        )
        atomic_write_json(self.store.task_dir(task_id) / "request.json", request)
        risk = classify_risk(**request["risk"])
        policy = {
            "risk": risk.level,
            "reasons": list(risk.reasons),
            "user_approval_required": risk.user_approval_required,
            "user_approved": bool(request["user_approved"]),
            "allowed_paths": list(request["allowed_paths"]),
        }

        def record_policy(state: dict) -> None:
            state["task_fingerprint"] = fingerprint
            state["policy"] = policy
            state["permission_policy"] = deepcopy(request["permission_policy"])
            state["progress_policy"] = deepcopy(request["progress_policy"])
            state["slug"] = slug
            state["phase"] = Phase.RISK_CHECK
            state["opencode"] = {
                "base_url": server_url,
                "requested_model": deepcopy(request.get("model")),
                "effort": request["effort"],
                "dispatch_marker": state["execution"]["dispatch_marker"],
                "dispatch_state": "NOT_STARTED",
                "dispatch_retry_count": 0,
            }

        self.store.update(task_id, record_policy)
        if risk.user_approval_required and not request["user_approved"]:
            return self.store.update(
                task_id,
                lambda current: current.update({"phase": Phase.AWAITING_APPROVAL}),
            )

        self.store.update(
            task_id, lambda current: current.update({"phase": Phase.PREPARING})
        )
        try:
            prepared = workspace.prepare(self.state_root, task_id, slug)
        except Exception as error:

            def fail(current: dict) -> None:
                current["phase"] = Phase.FAILED
                current["execution_state"] = ExecutionState.FAILED.value
                current["failure"] = {"message": str(error)}

            self.store.update(task_id, fail)
            raise

        def record_workspace(current: dict) -> None:
            current["worktree"] = {
                "path": str(prepared.path),
                "branch": prepared.branch,
                "base_sha": prepared.base_sha,
            }
            current["opencode"]["directory"] = str(prepared.path)

        return self.store.update(task_id, record_workspace)
