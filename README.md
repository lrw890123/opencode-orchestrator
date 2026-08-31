# OpenCode Orchestrator

这是一个本地 Codex Plugin：Codex 负责方案、风险判断和 Review，OpenCode 在隔离 worktree 中执行已批准的编码任务。等待期间由本地 MCP 进程监听 OpenCode SSE；不会周期性唤醒模型，也不需要 Codex 轮询。OpenCode 完成、提问或失败后，pending 工具返回，原 Codex 对话的同一回合继续。

Plugin 2.1.3 同时包含：自然语言 Skill、8 个 MCP 工具、本地控制 socket、状态迁移、工具卡进度通知和可回滚安装器。MCP 输出使用 `schema_version: 3`；默认 OpenCode 地址是 `http://127.0.0.1:4096`，默认 effort 是 `max`。

项目只使用 Python 标准库，没有运行时第三方依赖。版本变化见 [CHANGELOG.md](CHANGELOG.md)。

## 前置条件

- macOS/Linux 与 Python 3.10+
- Git
- 已启动的 OpenCode Server
- Codex Desktop/CLI 支持本地 Plugin 与 MCP

远程 OpenCode endpoint 默认被拒绝。provider、model 和 effort 在派发前向 OpenCode 校验，不可用时明确失败，不会静默换模型。

## 从源码安装

克隆仓库后，在仓库根目录执行两阶段安装。安装记录必须放在仓库之外或被忽略的 `install-records/` 目录；不要提交凭据、任务状态或 worktree 数据。

```bash
python3 scripts/install_plugin.py preinstall \
  --plugin-root "$PWD" \
  --codex-home "$CODEX_HOME" \
  --record "$PWD/install-records/current/install.json"

python3 scripts/install_plugin.py activate \
  --record "$PWD/install-records/current/install.json"
```

如果没有设置 `CODEX_HOME`，将命令中的 `$CODEX_HOME` 替换为实际 Codex 数据目录。安装完成后重启 Codex Desktop。

## 在对话框里使用

不需要记 MCP 工具名，直接表达意图即可。例如：

- “把这个需求交给 OpenCode，完成后你 review。”
- “用 `mcli/glm-5.3`，effort max。”
- “先停止等待，OpenCode 继续跑。”
- “继续等待刚才的任务。”
- “展示刚才任务的交互记录，不包含工具输出。”
- “告诉 OpenCode：请只修复这个测试，然后继续等待。”
- “中止刚才的 OpenCode 任务。”
- “收集结果，检查全部改动并跑测试。”

新任务使用 `delegate_and_wait`。等待被打断且 OpenCode 仍在执行时，使用同一 task ID 调用 `resume_wait`；它不会新建 session，也不会重发初始 prompt。如果任务处于 `PAUSED`、OpenCode 已 idle 且没有 pending input/tool，并且用户明确要求继续同一合同，可调用 `reply_and_wait` 的 `kind=continue`。它会先取得 WaitLease 并建立 SSE，再向原 session 发送带唯一 marker 的消息，同时复用原模型和 effort。不要绕过 MCP 直接调用内部客户端。`read_transcript` 只在需要时读取，默认排除推理和工具结果正文。

取消 MCP 工具或调用 `cancel_wait` 只停止本地等待，OpenCode 继续执行。只有明确调用 `abort_task` 才会请求 OpenCode 中止。多个活动任务并存时必须指定 task ID。

## 权限、进度与恢复策略

`delegate_and_wait` 可接收 `permission_policy` 与 `progress_policy`。省略时分别使用 `default=allow`、`persistence=task`，以及 `input_probe_interval_seconds=15`、`stall_timeout_seconds=600`。符合任务合同且不涉及高风险动作的权限会在任务范围内自动回复 `once`；`always` 只用于明确选择的 project persistence，并且必须同时提供 `user_approved=true` 与非空 `approval_basis`。

`external_directory` 是不可绕过的安全门：只有显式的绝对路径规则（例如 `/absolute/reference/**`）才能自动允许访问。未声明的外部目录、未知权限、目标不确定或高风险动作会返回 `INPUT_REQUIRED`，不会因为自然语言描述而自动放行。此时若用户明确批准 `once` 或 `always`，`reply_and_wait` 必须同时提交 `user_approved=true`，以及明确写出 permission 名称和目标 pattern 的 action-specific `approval_basis`；`reject` 不需要批准证据。

2.1.3 会合并 OpenCode session-scoped 与 legacy pending-input 接口并按 request/session 去重；任一接口仍有请求时都不会把它清空。与 permission `call_id` 关联的工具会显示为 `waiting_permission`，不再把尚未开始执行的命令误报成普通 `running`。

MCP 进程在现有 SSE 连接内执行本地 pending-input probe 与进度诊断；这些 probe 不调用模型，**不消耗 Codex token**。不要轮询 `task_status`，也不要用 shell 循环等待。`server.heartbeat` 只代表传输活动；长时间没有 meaningful progress 时结果为 `STALLED`，此时检查诊断、处理 pending input，或在进度恢复后对同一 task 调用 `resume_wait`。进入 `STALLED` 不会 abort、删除 worktree 或创建新 session；恢复始终复用同一 task/session/worktree（resume the same task/session/worktree）。

当 Codex 客户端为 MCP 调用提供 `progressToken` 时，等待工具会通过标准 `notifications/progress` 在工具卡中显示节流后的执行摘要。摘要只包含连接、分析/编辑、工具名称、工作树更新和等待输入等状态；不包含推理文本、命令参数、文件内容或工具输出。通知由本地 MCP 进程发送，不会唤醒 Codex 模型，也不会产生额外的状态轮询。

2.1.3 对外固定暴露 exactly eight public tools：`delegate_and_wait`、`reply_and_wait`、`resume_wait`、`task_status`、`read_transcript`、`collect_result`、`cancel_wait` 和 `abort_task`。`kind=continue` 复用 `reply_and_wait`，没有新增冗余工具；只允许 PAUSED + idle + 无 pending input/tool 的原任务，并通过 continuation marker 对不确定发送进行无重发恢复。单工具取消实验的结论是 `production_supported=false`，因此本版本不提供 `cancel_tool_call`；只有显式 `abort_task` 才改变 OpenCode 执行状态。

## Review 边界

完成后先调用 `collect_result`，以实际 changed/untracked files 和独立测试为准。需要返工时，用 `reply_and_wait` 在原 OpenCode session 发送精确 Review 意见，最多两轮。最终检查通过后，提交：

```json
{
  "tests_passed": true,
  "review_summary": "Codex inspected every changed file and reran the contract tests."
}
```

这只把 Review 推进到 `AWAITING_INTEGRATION`。Plugin 不自动 merge、push、发布、部署、cleanup 或删除 worktree。

## 外部终端控制

MCP 正在运行时，可从另一个终端操作：

```bash
python3 bin/oc-control status --task-id oc-...
python3 bin/oc-control cancel-wait --task-id oc-...
python3 bin/oc-control abort-task --task-id oc-...
```

也可直接执行有权限位的 `bin/oc-control`。只有一个活动任务时可省略 `--task-id`；多个任务时命令会拒绝猜测。

控制面为每个 MCP 进程使用独立的 `$CODEX_HOME/plugin-data/opencode-orchestrator/control/server-<pid>.sock` 与 `token-<pid>`，CLI 根据任务记录中的等待 owner PID 精确路由，因此多个 Codex MCP 进程可共享同一状态目录。如果自定义 state path 超过系统的 Unix socket 路径上限，会自动改用 `/tmp/opencode-orchestrator-<uid>/` 下由 state path 哈希与 PID 确定的短路径。两个目录权限均为 `0700`，socket 和每次 MCP 启动生成的随机 token 文件权限为 `0600`；请求还带一次性 nonce。token 不进入任务日志或模型上下文。MCP 不在线时，`status` 直接读本地状态，`cancel-wait` 只修复死进程留下的监听状态，`abort-task` 先保存 abort intent 再尝试联系 OpenCode。

## 数据与恢复

默认数据目录（task state `schema_version: 3`；旧 v2 记录会复制式迁移）：

```text
$CODEX_HOME/plugin-data/opencode-orchestrator/
├── tasks/<task-id>/state.json
├── tasks/<task-id>/request.json
├── tasks/<task-id>/events.jsonl
├── tasks/<task-id>/transcript.json
└── control/
```

可以用 `OPENCODE_ORCHESTRATOR_STATE_ROOT` 指定其他目录。旧版独立 Skill 数据默认位于 `$CODEX_HOME/opencode-orchestrator`；首次使用默认路径时执行复制式迁移，源目录不会被删除或覆盖。

若 Codex/MCP 在等待时退出，OpenCode 不会被自动 abort。下一次启动会把死 owner 的 `ATTACHED` 修复为 `DETACHED`，先诊断 pending permission/question，再用同一 task ID、同一 OpenCode session 调用 `resume_wait`。dispatch marker 和 task fingerprint 防止恢复时重复发送初始任务；不要编辑原始 state，也不要绕过 MCP 服务。

## 安装、重启与回滚

安装分为两个可恢复阶段。先选择一个不会覆盖的 install record 路径：

```bash
python3 scripts/install_plugin.py preinstall \
  --plugin-root /absolute/path/to/opencode-orchestrator \
  --codex-home /absolute/path/to/.codex \
  --record /absolute/path/to/install-record/install.json

python3 scripts/install_plugin.py activate \
  --record /absolute/path/to/install-record/install.json
```

`preinstall` 校验包结构、90000 秒 MCP tool timeout，并从临时缓存副本完成 initialize/tools-list 握手；它不会移动旧 Skill。`activate` 先安装并确认 Plugin 可见，再把旧独立 Skill 移入 record 目录下的备份，备份不会自动删除。

完成 `activate` 后必须重启 Codex Desktop。重启会终止当时活动回合，因此不要在有 pending 工具时安装。重启后回到原对话，发起一次真实 `delegate_and_wait` 验证自动唤醒。

需要回滚时：

```bash
python3 scripts/install_plugin.py rollback \
  --record /absolute/path/to/install-record/install.json
```

回滚按 mutation log 恢复旧 Skill、移除本次安装的 Plugin，并且只在本次新增 marketplace 时移除它。任务状态、OpenCode session 和 worktree 始终保留。

## 验证

仅生成一次性 Git fixture、不联系 OpenCode：

```bash
python3 scripts/live_plugin_e2e.py \
  --dry-run \
  --state-root /absolute/path/to/e2e/state \
  --model mcli/glm-5.3 \
  --effort max
```

连接真实 OpenCode：

```bash
python3 scripts/live_plugin_e2e.py \
  --state-root /absolute/path/to/e2e/state \
  --server http://127.0.0.1:4096 \
  --model mcli/glm-5.3 \
  --effort max
```

报告包含 task/session ID、模型与 effort、MCP/delegate/prompt 次数、diff、独立测试、transcript、最终 Review 状态，以及未 merge/push/cleanup 的边界断言。
