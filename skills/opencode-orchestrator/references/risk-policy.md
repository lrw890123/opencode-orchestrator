# Risk policy

Classify the task before creating its execution worktree. Approval for implementation never grants authority for a separate high-risk action.

## Low risk

All conditions hold:

- One module or a clearly bounded area.
- At most 5 files and at most 300 estimated changed lines.
- No public-interface, persisted-format, deployment, or dependency change.
- No external side effect; Git can reverse the work.

Codex may delegate and may answer reversible read, test, and scoped-edit requests that remain within the task contract.

## Large change

Any condition holds:

- More than 5 files or more than 300 estimated lines.
- Cross-module or architectural work.
- Public interface, configuration format, or persisted format changes.
- Dependency addition, removal, or upgrade.

Obtain user approval before dispatch. If the actual change exceeds the approved estimate or allowlist, stop automatic execution and surface the difference.

## High risk

Always obtain explicit user authorization immediately before the action:

- Destructive deletion, overwrite, or bulk movement of important data.
- Production deployment, remote writes, external messages, or paid actions.
- Database or irreversible data migration.
- Credentials, authentication, authorization, or security-policy changes.
- Git history rewrite, force push, or protected-branch integration.
- Any action with unclear impact or difficult recovery.

Do not treat OpenCode automation, a prior broad approval, or a low-risk coding contract as authorization for these actions.

Cancellation is not a high-risk mutation: `cancel_wait` only detaches Codex from the local SSE wait. `abort_task` changes OpenCode execution state and must always be an explicit action; it still does not authorize cleanup, merge, push, or deployment.

## Permission and progress gates

The task policy defaults to `permission_policy.default=allow` and `permission_policy.persistence=task`; this is a bounded, task-local default, not blanket authorization. A task-local allow is answered with `once`. An `external_directory` request requires an explicit absolute-path `allow` rule (for example `/absolute/reference/**`). Unknown, indeterminate, high-risk, out-of-contract, and undeclared external requests fail closed to user input.

Project persistence is a separate decision: `always` is project-persistent and requires `persistence=project`, explicit user approval, and a non-empty `approval_basis`. Never infer it from task text or silently create saved OpenCode permissions.

Progress probes run locally inside the MCP wait, do not invoke a model, and **不消耗 Codex token**. Do not poll `task_status`. Heartbeats are transport liveness only; a heartbeat-only wait may return `STALLED` for diagnostics without aborting OpenCode. Recovery uses `resume_wait` on the same task/session/worktree, with no replacement session or automatic cleanup.
