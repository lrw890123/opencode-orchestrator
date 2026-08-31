---
name: opencode-orchestrator
description: Delegate approved Git coding tasks to OpenCode through bundled MCP tools, resume zero-poll waits, answer input, collect results, and review changes. Use when the user asks Codex and OpenCode to collaborate on implementation.
---

# OpenCode Orchestrator

Use Codex to define scope, make risk decisions, and review. Use the bundled MCP tools to isolate and execute the approved work in OpenCode.

## Delegate a new task

1. Resolve the absolute Git repository path and the requested scope. Do not delegate a non-Git write task without an explicit isolation decision from the user.
2. Read [references/risk-policy.md](references/risk-policy.md) and classify the proposed work. Obtain explicit approval before a large change or any high-risk action.
3. Read [references/task-contract.md](references/task-contract.md) and construct a complete `delegate_and_wait` input. Preserve an explicitly requested provider/model and effort; default effort to `max` and never silently substitute an unavailable selection.
4. Call `delegate_and_wait` once. Let its pending MCP call wait for OpenCode and wake this same Codex turn. Do not poll `task_status`, run shell wait loops, or stream raw SSE/reasoning into the conversation.

The optional `permission_policy` defaults to `default=allow` with `persistence=task`; safe contract-compliant permissions receive the task-local `once` reply. An `external_directory` request is allowed automatically only by an explicit absolute rule such as `/absolute/reference/**`. Unknown, high-risk, indeterminate, or undeclared external access returns for user input. To approve a sensitive request with either `once` or `always`, pass `user_approved=true` and an action-specific `approval_basis` that names the permission and exact target pattern; `reject` needs no approval evidence. `always` is project-persistent.

Pending permissions/questions are reconciled across both OpenCode discovery APIs and deduplicated by request/session. Never infer that input disappeared from only one empty API response. A pending tool linked by permission `call_id` is reported as `waiting_permission`, which means the command has not started even if OpenCode's raw tool part says `running`.

If the user directly continues the same OpenCode session after the task was recorded as `COMPLETED` or `ABORTED`, `task_status` may project live `RUNNING` or `INPUT_REQUIRED` state without mutating the record. Reacquire it only through `resume_wait` or an exact still-pending permission/question reply. Those lease-owned operations preserve the task/session/worktree and invalidate stale review state. An earlier abort is marked `SUPERSEDED` only after new live activity is detected; idle sessions, heartbeats, and stale tool records do not reopen it.

For a manually approved permission, `remember_for_task=true` may accompany `response=once`, `user_approved=true`, and an action-specific `approval_basis`. It stores only the exact permission and patterns from the live request as task-local rules. It must never accept a caller-supplied broader pattern or override a deny or non-bypassable safety gate.

The optional `progress_policy` defaults to a 15-second input probe and a 600-second stall timeout. Probes are local MCP work inside the existing SSE wait, invoke no model, and **不消耗 Codex token**. Do not use `task_status` polling as a progress mechanism.

When the Codex client supplies an MCP `progressToken`, the pending tool card receives throttled `notifications/progress` summaries for connection, analysis/editing, safe tool names, worktree updates, and input waits. These notifications expose no reasoning text, command arguments, file content, or tool output; they do not wake the Codex model or add polling.

If multiple tasks are active, identify them by task ID and ask the user which one they mean. Never guess.

## Handle a returned outcome

| Outcome | Next action |
| --- | --- |
| `COMPLETED` | Call `collect_result`, then inspect and test the actual worktree changes. |
| `INPUT_REQUIRED` | Answer only when the contract and risk policy make the answer unambiguous. Use `reply_and_wait`; otherwise ask the user. |
| `INTERRUPTED` or `WAIT_CANCELLED` | Use `resume_wait` with the same task ID. Never create a replacement session or resend the initial task. |
| `FAILED` | Preserve the task and worktree, read the failure/transcript as needed, and diagnose before proposing a retry. |
| `ABORTED` | Report the partial state. Reattach only if the user directly started new live activity in the same session and explicitly asks to resume or answer its pending input. |
| `STALLED` | Inspect diagnostics, surface pending input, or resume the same task after progress. Do not abort or create a replacement session. |

Use `task_status` for a single diagnostic snapshot, not periodic monitoring. Use `read_transcript` only when the user asks to see the interaction or when specific execution evidence is needed; tool output bodies remain opt-in.

When the user explicitly asks to continue the same approved task after a local interruption, use `reply_and_wait` with `kind=continue` only if the persisted phase is `PAUSED`, OpenCode is idle, and there are no pending permissions, questions, or tools. The tool rechecks all gates, acquires the existing task's WaitLease, opens SSE before sending, reuses the original model/effort, and records a unique continuation marker for no-resend recovery. If OpenCode is busy, use `resume_wait` instead. Never call the internal OpenCode client directly.

## Cancellation and external control

MCP tool cancellation and `cancel_wait` stop only the local wait. OpenCode keeps running and the same task can later be resumed. Only an explicit `abort_task` call aborts the OpenCode session. Never turn cancellation into abort.

Single-tool cancellation is not a production capability in 2.1.5: the disposable experiment records `production_supported=false`. The public surface remains exactly eight tools; `kind=continue` reuses `reply_and_wait`, and recovery otherwise uses a permission reply, `resume_wait`, or explicit `abort_task`.

The external `bin/oc-control` command can show status, cancel a pending wait, or explicitly abort a task. It never deletes task data or worktrees.

## Review the result

After `collect_result`, treat the changed and untracked files plus independently run tests as authoritative. OpenCode's summary is supporting evidence.

The package exposes exactly eight public MCP tools and every tool output uses `schema_version: 3`. A stalled or interrupted wait keeps the same task/session/worktree; recovery must not merge, push, publish, deploy, clean up, or delete a worktree.

Inspect every changed file, enforce the allowed-path contract, and run the contract's exact test commands. If revision is needed, call `reply_and_wait` with `kind=review` in the same task/session, then collect again. Stop after two review rounds or whenever scope or authority changes.

After all changes are inspected and the tests pass, call `collect_result` with `review_evidence` containing `tests_passed: true` and a concrete `review_summary`. This advances the review record only. Never merge, push, publish, deploy, clean up, or delete a worktree unless the user explicitly asks for that exact action.
