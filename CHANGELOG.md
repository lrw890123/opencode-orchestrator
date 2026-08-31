# Changelog

All notable changes to OpenCode Orchestrator are documented here.

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
