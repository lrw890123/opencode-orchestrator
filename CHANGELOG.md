# Changelog

All notable changes to OpenCode Orchestrator are documented here.

## Unreleased

## 2.1.6

- Reconcile a running or stalled task to `COMPLETED` from durable transcript
  evidence: when the transcript ends on a completed assistant turn with a
  `step-finish` and no input is pending, `collect_result` adopts the finished
  turn even if the session still reports `busy` and no `session.idle` ever
  reached the orchestrator's event stream. This covers sessions driven from
  another OpenCode process (shared storage, per-process event bus).
- Allow `reply_and_wait` with `kind=continue` on a `STALLED` task so a wedged
  turn can be nudged with a continuation prompt once the stall threshold fires.

## 2.1.5

- Let `reply_and_wait` with `kind=continue` re-acquire a `COMPLETED` or
  `ABORTED` task in its original OpenCode session when
  `payload.reacquire=true`, superseding the terminal record and invalidating
  stale review state. This unblocks fully agent-driven recovery without a
  human message in OpenCode.
- Let `resume_wait` block on a terminal task's session and adopt new live
  activity as it arrives instead of returning the terminal state immediately.
- Let `collect_result` reacquire an externally continued turn that already
  completed before Codex resumed, while preserving the original task, session,
  and worktree.
- Reject collection while the external turn is still running or awaiting input,
  and return actionable MCP input errors instead of generic internal errors.

## 2.1.4

- Detect direct user continuations in an existing OpenCode session after the
  orchestrator recorded `COMPLETED` or `ABORTED`.
- Project live `RUNNING`/`INPUT_REQUIRED` state from `task_status` without
  making the read-only status tool mutate task records.
- Let `resume_wait` and exact live permission/question replies reacquire the
  original task, session, and worktree under the existing wait lease.
- Optionally remember explicitly approved live permission patterns as exact
  task-local rules; these rules never override a policy denial.

## 2.1.3

- Continue an eligible paused OpenCode session through the existing `reply_and_wait` tool.
- Reuse the original task, session, worktree, model, and effort.
- Recover uncertain continuation delivery without resending the message.

## 2.1.2

- Reconcile OpenCode permission requests across the current and legacy pending-input APIs.
- Report tools blocked on permission as `waiting_permission` instead of `running`.

## 2.1.1

- Stream redacted OpenCode progress summaries to the Codex tool card.
- Keep progress delivery local so it does not wake the model or consume model tokens.

## 2.1.0

- Package the orchestrator as a Codex plugin with eight public MCP tools.
- Add isolated Git worktrees, SSE waits, persistent task state, external wait control,
  permission policies, stall detection, recovery, review gates, and rollback-safe installation.
