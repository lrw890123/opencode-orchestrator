from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any

from .permission_policy import normalize_permission_policy, normalize_progress_policy
from .task_state import TaskLock, TaskLockError, atomic_write_json, utc_now


EXECUTION_BY_PHASE = {
    "DRAFT": "PREPARING",
    "RISK_CHECK": "PREPARING",
    "AWAITING_APPROVAL": "PREPARING",
    "PREPARING": "PREPARING",
    "DISPATCHED": "PREPARING",
    "RUNNING": "RUNNING",
    "NEEDS_INPUT": "INPUT_REQUIRED",
    "PERMISSION_WAIT": "INPUT_REQUIRED",
    "PAUSED": "RUNNING",
    "COLLECTING": "COMPLETED",
    "REVIEWING": "COMPLETED",
    "REVISION_REQUESTED": "RUNNING",
    "PASSED": "COMPLETED",
    "AWAITING_INTEGRATION": "COMPLETED",
    "FAILED": "FAILED",
    "CANCELLED": "ABORTED",
}

REVIEW_BY_PHASE = {
    "COLLECTING": "READY",
    "REVIEWING": "REVIEWING",
    "REVISION_REQUESTED": "REVISION_REQUESTED",
    "PASSED": "PASSED",
    "AWAITING_INTEGRATION": "AWAITING_INTEGRATION",
}


def _migration_diagnostic(code: str, **details: Any) -> dict[str, Any]:
    """Return a deterministic diagnostic that cannot contain event bodies."""

    diagnostic: dict[str, Any] = {"kind": "migration", "code": code}
    diagnostic.update(details)
    return diagnostic


def _merge_diagnostic(
    first: dict[str, Any] | None,
    second: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if first is None:
        return deepcopy(second) if second is not None else None
    if second is None or first == second:
        return deepcopy(first)

    def codes(value: dict[str, Any]) -> set[str]:
        code = value.get("code", "unknown")
        if code == "multiple" and isinstance(value.get("codes"), list):
            return {str(item) for item in value["codes"]}
        return {str(code)}

    merged_codes = sorted(codes(first) | codes(second))
    details: dict[str, Any] = {}
    indices = sorted(
        {
            int(item)
            for value in (first, second)
            for item in (value.get("indices") or [])
            if isinstance(item, int) and not isinstance(item, bool)
        }
    )
    lines = sorted(
        {
            int(item)
            for value in (first, second)
            for item in (value.get("lines") or [])
            if isinstance(item, int) and not isinstance(item, bool)
        }
    )
    if indices:
        details["indices"] = indices
    if lines:
        details["lines"] = lines
    if len(merged_codes) == 1:
        return _migration_diagnostic(merged_codes[0], **details)
    # Keep the diagnostic projection bounded and free of arbitrary persisted
    # values.  The individual diagnostics produced by this module are already
    # sanitized, so only their stable codes and line/index metadata survive.
    return _migration_diagnostic("multiple", codes=merged_codes, **details)


def _event_type(record: dict[str, Any]) -> str | None:
    event = record.get("event") if "event" in record else record
    if not isinstance(event, dict):
        return None
    value = event.get("type")
    return value if isinstance(value, str) and value.strip() else None


def _event_recorded_at(record: dict[str, Any]) -> tuple[str, datetime] | None:
    value = record.get("recorded_at")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return value, parsed.astimezone(timezone.utc)


def _is_heartbeat(event_type: str) -> bool:
    return event_type in {"server.heartbeat", "server.connected"} or event_type.endswith(
        (".heartbeat", ".connected")
    )


def _latest_progress_event(
    event_records: Sequence[dict[str, Any]],
) -> tuple[str, str] | None:
    newest: tuple[datetime, int, str, str] | None = None
    for index, record in enumerate(event_records):
        if not isinstance(record, dict):
            continue
        event_type = _event_type(record)
        if event_type is None or _is_heartbeat(event_type):
            continue
        recorded = _event_recorded_at(record)
        if recorded is None:
            continue
        timestamp, parsed = recorded
        candidate = (parsed, index, timestamp, event_type)
        if newest is None or candidate[:2] >= newest[:2]:
            newest = candidate
    if newest is None:
        return None
    return newest[2], newest[3]


def _event_record_diagnostic(
    event_records: Sequence[object],
) -> dict[str, Any] | None:
    diagnostic: dict[str, Any] | None = None
    for index, record in enumerate(event_records):
        if not isinstance(record, dict):
            issue = "malformed_event_record"
        else:
            issue = _event_record_issue(record)
        if issue is None:
            continue
        diagnostic = _merge_diagnostic(
            diagnostic, _migration_diagnostic(issue, indices=[index])
        )
    return diagnostic


def _event_record_issue(record: dict[str, Any]) -> str | None:
    """Classify malformed event metadata without inspecting/exposing bodies."""

    if not isinstance(record, dict):
        return "malformed_event_record"
    if "event" in record:
        if not isinstance(record.get("event"), dict):
            return "malformed_event_record"
    if _event_recorded_at(record) is None:
        return "invalid_event_recorded_at"
    if _event_type(record) is None:
        return "invalid_event_type"
    return None


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def _read_event_records(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    records: list[dict[str, Any]] = []
    diagnostic: dict[str, Any] | None = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return records, None
    except UnicodeDecodeError:
        return records, _migration_diagnostic(
            "invalid_event_encoding", lines=[]
        )
    except OSError:
        return records, None
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            diagnostic = _merge_diagnostic(
                diagnostic,
                _migration_diagnostic("malformed_event_json", lines=[line_number]),
            )
            continue
        if not isinstance(value, dict):
            diagnostic = _merge_diagnostic(
                diagnostic,
                _migration_diagnostic("malformed_event_json", lines=[line_number]),
            )
            continue
        issue = _event_record_issue(value)
        if issue is not None:
            diagnostic = _merge_diagnostic(
                diagnostic,
                _migration_diagnostic(issue, lines=[line_number]),
            )
            continue
        records.append(value)
    return records, diagnostic


def migrate_v1_state(state: dict[str, Any]) -> dict[str, Any]:
    if state.get("schema_version") == 2:
        return deepcopy(state)
    if state.get("schema_version") != 1:
        raise ValueError(f"unsupported task schema: {state.get('schema_version')}")
    phase = state.get("phase")
    if phase not in EXECUTION_BY_PHASE:
        raise ValueError(f"unsupported v1 phase: {phase}")
    migrated = deepcopy(state)
    migrated["schema_version"] = 2
    migrated["legacy_phase"] = phase
    migrated["execution_state"] = EXECUTION_BY_PHASE[phase]
    migrated["wait_state"] = "DETACHED"
    migrated["review_state"] = REVIEW_BY_PHASE.get(phase, "PENDING")
    migrated.setdefault("wait", {})
    migrated.setdefault("task_fingerprint", None)
    return migrated


def migrate_task_record(
    state: dict[str, Any],
    request: dict[str, Any] | None,
    event_records: Sequence[dict[str, Any]] = (),
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Purely migrate one task state to schema v3.

    The returned diagnostic is intentionally a small metadata projection.  It
    is used by startup migration to record malformed persisted event entries
    without ever copying their JSON bodies into task state.
    """

    if not isinstance(state, dict):
        raise ValueError("task state must be an object")
    if request is not None and not isinstance(request, dict):
        raise ValueError("task request must be an object or null")

    version = state.get("schema_version")
    if version == 1:
        migrated = migrate_v1_state(state)
    elif version == 2 or version == 3:
        migrated = deepcopy(state)
    else:
        raise ValueError(f"unsupported task schema: {version}")

    diagnostic = _event_record_diagnostic(event_records)
    request_value = request or {}

    # State is authoritative when it already contains a v3 policy.  For v1/v2
    # records, use the policy persisted in the request and then fall back to
    # the safe Task-1 defaults.  Never infer an external-directory allow rule
    # from the legacy contract's ordinary allowed_paths field.
    state_permission_present = "permission_policy" in migrated
    if version == 3:
        # A v3 state is authoritative.  Recovering a missing/corrupt state
        # policy from request.json could silently restore broader authority.
        permission_present = state_permission_present
        permission_value = migrated.get("permission_policy")
    elif state_permission_present:
        permission_present = True
        permission_value = migrated["permission_policy"]
    else:
        permission_present = "permission_policy" in request_value
        permission_value = request_value.get("permission_policy")
    try:
        if (permission_present and permission_value is None) or (
            not permission_present and version == 3
        ):
            raise ValueError("persisted permission policy is missing or null")
        permission_policy = normalize_permission_policy(permission_value)
    except ValueError:
        # Only an actually missing legacy policy receives the historical
        # default allow.  Once a persisted/request policy is present, corrupt
        # contents must not broaden authority during migration.
        permission_policy = normalize_permission_policy({"default": "ask"})
        diagnostic = _merge_diagnostic(
            diagnostic, _migration_diagnostic("invalid_permission_policy")
        )

    progress_value = migrated.get("progress_policy")
    if progress_value is None:
        progress_value = request_value.get("progress_policy")
    try:
        progress_policy = normalize_progress_policy(progress_value)
    except ValueError:
        progress_policy = normalize_progress_policy(None)
        diagnostic = _merge_diagnostic(
            diagnostic, _migration_diagnostic("invalid_progress_policy")
        )

    migrated["schema_version"] = 3
    migrated["permission_policy"] = permission_policy
    migrated["progress_policy"] = progress_policy
    if "permission_audit" not in migrated or not isinstance(
        migrated.get("permission_audit"), list
    ):
        migrated["permission_audit"] = []

    progress = migrated.get("progress")
    if not isinstance(progress, dict):
        progress = {}
    else:
        progress = deepcopy(progress)

    # For a v1/v2 record, the event log is the best available progress source.
    # Once a v3 baseline exists, preserve it exactly so this function remains
    # idempotent and does not move a task's progress clock backwards.
    baseline = _latest_progress_event(event_records)
    if version in {1, 2} or not progress.get("last_progress_at"):
        if baseline is not None:
            progress["last_progress_at"], progress["last_progress_event"] = baseline
        else:
            progress["last_progress_at"] = (
                migrated.get("updated_at")
                or migrated.get("created_at")
                or utc_now()
            )
            progress["last_progress_event"] = "task.migrated"

    progress.setdefault("last_progress_at", migrated.get("updated_at") or utc_now())
    progress.setdefault("last_progress_event", "task.migrated")
    progress.setdefault("idle_seconds", 0)
    progress.setdefault("heartbeat_count", 0)
    progress.setdefault("pending_tools", [])
    progress.setdefault("pending_permissions", [])
    progress.setdefault("pending_questions", [])
    progress.setdefault("diagnostic_error", None)
    progress.setdefault("last_input_probe_at", None)
    if diagnostic is not None:
        existing = progress.get("diagnostic_error")
        if isinstance(existing, dict) and existing.get("kind") == "migration":
            progress["diagnostic_error"] = _merge_diagnostic(existing, diagnostic)
        elif existing is None:
            progress["diagnostic_error"] = deepcopy(diagnostic)
    migrated["progress"] = progress
    return migrated, diagnostic


def migrate_task_records(state_root: Path) -> dict[str, int]:
    """Migrate every task state in ``state_root`` under its task lock.

    The operation is deliberately in-place only for ``state.json`` and uses
    ``atomic_write_json`` for every changed record.  Requests, event logs,
    sessions, and worktree artifacts are read-only inputs to migration.
    """

    root = Path(state_root).expanduser().resolve()
    tasks_root = root / "tasks"
    counts = {"examined": 0, "migrated": 0, "unchanged": 0}
    if not tasks_root.is_dir():
        return counts

    for task_dir in sorted(path for path in tasks_root.iterdir() if path.is_dir()):
        state_path = task_dir / "state.json"
        if not state_path.is_file():
            continue
        task_lock = TaskLock(task_dir / "task.lock")
        try:
            task_lock.__enter__()
        except (TaskLockError, OSError):
            # A live task update owns this lock.  Leave it untouched and let a
            # later startup retry rather than racing its atomic write.
            continue
        try:
            try:
                with state_path.open(encoding="utf-8") as handle:
                    state = json.load(handle)
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(state, dict):
                continue
            counts["examined"] += 1

            request = _read_json_object(task_dir / "request.json")
            event_records, event_diagnostic = _read_event_records(
                task_dir / "events.jsonl"
            )
            migrated, diagnostic = migrate_task_record(
                state, request, event_records
            )
            diagnostic = _merge_diagnostic(diagnostic, event_diagnostic)
            if diagnostic is not None:
                progress = migrated.setdefault("progress", {})
                if not isinstance(progress, dict):
                    progress = {}
                    migrated["progress"] = progress
                existing = progress.get("diagnostic_error")
                if isinstance(existing, dict) and existing.get("kind") == "migration":
                    progress["diagnostic_error"] = _merge_diagnostic(existing, diagnostic)
                elif existing is None:
                    progress["diagnostic_error"] = deepcopy(diagnostic)

            if migrated == state:
                counts["unchanged"] += 1
            else:
                atomic_write_json(state_path, migrated)
                counts["migrated"] += 1
        finally:
            task_lock.__exit__(None, None, None)
    return counts


def resolve_state_roots(environ: Mapping[str, str] | None = None) -> tuple[Path, Path]:
    values = os.environ if environ is None else environ
    codex_home = Path(values.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()
    explicit = values.get("OPENCODE_ORCHESTRATOR_STATE_ROOT")
    target = (
        Path(explicit).expanduser().resolve()
        if explicit
        else (codex_home / "plugin-data" / "opencode-orchestrator").resolve()
    )
    legacy = (codex_home / "opencode-orchestrator").resolve()
    return target, legacy


def _copy_without_overwrite(source: Path, destination: Path) -> None:
    if destination.exists():
        if destination.is_file() and destination.read_bytes() == source.read_bytes():
            return
        raise FileExistsError(f"migration destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _migrate_state_root_unlocked(legacy_root: Path, target_root: Path) -> dict[str, Any]:
    legacy = Path(legacy_root).expanduser().resolve()
    target = Path(target_root).expanduser().resolve()
    if legacy == target:
        raise ValueError("legacy and target state roots must be different")
    marker_path = target / "migration.json"
    if marker_path.is_file():
        with marker_path.open(encoding="utf-8") as handle:
            marker = json.load(handle)
        migrate_task_records(target)
        marker["already_migrated"] = True
        return marker
    if not legacy.is_dir():
        raise FileNotFoundError(f"legacy state root does not exist: {legacy}")
    source_digest = _tree_digest(legacy)

    target.mkdir(parents=True, exist_ok=True)
    config = legacy / "config.json"
    if config.is_file():
        _copy_without_overwrite(config, target / "config.json")

    migrated_tasks = 0
    legacy_tasks = legacy / "tasks"
    if legacy_tasks.is_dir():
        for source_task in sorted(path for path in legacy_tasks.iterdir() if path.is_dir()):
            source_state = source_task / "state.json"
            if not source_state.is_file():
                continue
            with source_state.open(encoding="utf-8") as handle:
                state = json.load(handle)
            task_id = state.get("task_id")
            if task_id != source_task.name:
                raise ValueError(f"task directory does not match state: {source_task}")
            target_task = target / "tasks" / task_id
            target_task.mkdir(parents=True, exist_ok=True)
            target_state = target_task / "state.json"
            request = _read_json_object(source_task / "request.json")
            event_records, event_diagnostic = _read_event_records(
                source_task / "events.jsonl"
            )
            expected_state, diagnostic = migrate_task_record(
                state, request, event_records
            )
            diagnostic = _merge_diagnostic(diagnostic, event_diagnostic)
            if diagnostic is not None:
                progress = expected_state.setdefault("progress", {})
                existing = progress.get("diagnostic_error")
                if isinstance(existing, dict) and existing.get("kind") == "migration":
                    progress["diagnostic_error"] = _merge_diagnostic(existing, diagnostic)
                elif existing is None:
                    progress["diagnostic_error"] = deepcopy(diagnostic)
            if target_state.is_file():
                with target_state.open(encoding="utf-8") as handle:
                    existing_state = json.load(handle)
                if existing_state != expected_state:
                    raise FileExistsError(f"migrated task state conflicts: {target_state}")
            else:
                atomic_write_json(target_state, expected_state)
            for source_file in sorted(path for path in source_task.iterdir() if path.is_file()):
                if source_file.name in {"state.json", "task.lock", "wait.lock"}:
                    continue
                _copy_without_overwrite(source_file, target_task / source_file.name)
            migrated_tasks += 1

    # Migrate any pre-existing target tasks as well as records copied above.
    # This keeps direct callers of migrate_state_root safe; prepare_state_root
    # repeats the idempotent pass before constructing services.
    migrate_task_records(target)

    marker = {
        "migration_version": 1,
        "source_root": str(legacy),
        "target_root": str(target),
        "source_schema": 1,
        "target_schema": 3,
        "migrated_tasks": migrated_tasks,
        "source_digest": source_digest,
        "migrated_at": utc_now(),
        "already_migrated": False,
    }
    atomic_write_json(marker_path, marker)
    return marker


def migrate_state_root(legacy_root: Path, target_root: Path) -> dict[str, Any]:
    target = Path(target_root).expanduser().resolve()
    with TaskLock(target / "migration.lock"):
        return _migrate_state_root_unlocked(legacy_root, target)
