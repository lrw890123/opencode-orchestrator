# Changelog

All notable changes to OpenCode Orchestrator are documented here.

## 2.1.5

- Do not mistake an abort-generated terminal message for external session activity.
- Direct projected external completions to `resume_wait` before collection or review.
- Split task preparation, pending input, progress recovery, and result review out of the core service.
- Remove the obsolete bridge CLI and unused transition facade.

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
