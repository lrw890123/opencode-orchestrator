# MCP task contract

Create a complete `delegate_and_wait` input before dispatch:

```json
{
  "repo_path": "/absolute/path/to/repo",
  "task_contract": {
    "goal": "Concrete outcome",
    "non_goals": ["Explicit exclusion"],
    "approved_plan": ["Ordered implementation step"],
    "allowed_paths": ["src/**", "tests/**"],
    "forbidden_actions": ["Do not change dependencies"],
    "acceptance_criteria": ["Observable passing condition"],
    "test_commands": ["python3 -m unittest discover -s tests -t . -v"],
    "risk": {
      "file_count": 2,
      "line_count": 80,
      "cross_module": false,
      "public_interface": false,
      "dependency_change": false,
      "high_risk_actions": []
    },
    "user_approved": false
  },
  "model": {
    "providerID": "mcli",
    "modelID": "glm-5.3"
  },
  "effort": "max",
  "timeout_seconds": 3600
}
```

The two policy objects are optional top-level fields on the same `delegate_and_wait` input. This complete example uses the scalar defaults and adds one explicit external-directory rule:

```json
{
  "permission_policy": {
    "default": "allow",
    "persistence": "task",
    "approval_basis": null,
    "rules": [
      {
        "permission": "external_directory",
        "pattern": "/absolute/reference/**",
        "action": "allow"
      }
    ]
  },
  "progress_policy": {
    "input_probe_interval_seconds": 15,
    "stall_timeout_seconds": 600
  }
}
```

Omitted policies use these defaults: `permission_policy.default=allow`, `permission_policy.persistence=task`, `progress_policy.input_probe_interval_seconds=15`, and `progress_policy.stall_timeout_seconds=600`. Task persistence replies with `once` and is recorded only in task-local audit state. Internal pending-input and progress probes are local HTTP/SSE work in the MCP process, do not invoke a model, and **不消耗 Codex token**; never replace the pending wait with `task_status` polling.

`external_directory` access requires an explicit matching rule with an absolute path pattern. Unknown, high-risk, indeterminate, out-of-contract, or undeclared external access returns for user input (`INPUT_REQUIRED`) instead of being auto-allowed. `always` is project-persistent and explicitly approved: it requires `persistence=project`, a deliberate user selection, and a non-empty `approval_basis`.

Requirements:

- Use an absolute Git repository path.
- Make the goal, allowed paths, acceptance criteria, and test commands concrete and repository-specific.
- Put no secrets in commands or task text.
- `model` is optional. When the user writes `provider/model`, split only at the first `/` and preserve both identifiers exactly.
- `effort` defaults to `max`. The Plugin validates it against the selected model and never falls back silently.
- `timeout_seconds` must be from 1 through 86400.
- Set `user_approved` true only after the user approves a large task. High-risk actions still require action-specific approval.
- OpenCode receives the task ID, dispatch marker, fingerprint, base commit, and reporting format automatically.
- Review feedback stays in the original OpenCode session and may not expand the approved goal or allowlist.

## Reply payloads

- Review: `{"task_id":"...","kind":"review","payload":{"text":"precise feedback"}}`
- Permission: `{"task_id":"...","kind":"permission","payload":{"request_id":"...","response":"once"}}`
- Question: `{"task_id":"...","kind":"question","payload":{"request_id":"...","answers":[["answer"]]}}`

An `always` permission requires explicit user approval and a non-empty approval basis. Use `reject` when a request exceeds the contract.

The public MCP output is `schema_version: 3`. A `STALLED` outcome means that transport heartbeats continued without meaningful progress; inspect diagnostics, surface pending input, or resume the same task after progress. Stall detection never aborts OpenCode and never creates a replacement session; `resume_wait` must reuse the existing task, session, and worktree.

## Completion sequence

Use one pending `delegate_and_wait` call; never replace it with repeated `task_status` calls. After completion, collect and test, send review feedback in the same task when needed, then collect with review evidence. Reading the transcript is optional and must not be used as a polling mechanism.
