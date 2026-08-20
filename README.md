# 项目介绍
本项目基于learn-claude-code的s15 integrated_harness 
https://github.com/shareAI-lab/learn-claude-code

由于项目距离真正的生产级别的coding agent还有很大距离，所以此项目是在learn-claude-code的基础上，完善学习。
目标：生产级的coding工具

新增功能
1. web ui页面，增加了web的交互，启动命令： python -m coding_assistant.web --host 127.0.0.1 --port 8787
2. 调用链路，可以查看每次 call_llm 的完整输入、模型响应和 tools_result。

# Agent Harness 集成 — 多种机制，一个循环

> *"多种机制，一个循环"* — 工具、权限、记忆、任务、团队、插件都挂在同一个 while True 上。
>
> **Harness 层**: 集成 — 把本章示例实际使用的机制放进同一个可运行系统。

---

## 浏览器对话工作台

除了 CLI 交互外，现在可以启动一个本地浏览器页面：

```bash
python -m coding_assistant.web --host 127.0.0.1 --port 8787
# 或安装项目后：coding-assistant-web
```

打开 `http://127.0.0.1:8787` 后可以：

- 在左侧查看历史对话，并新建多个独立会话；
- 为每个会话设置工作目录，文件工具会在该目录内执行；
- 在“调试 Trace”标签中查看每次模型调用的 prompt hash、工具集合、token、缓存指标和响应；
- 查看每个工具调用及对应的 `tools_result`，包括阻止、后台任务和错误信息。

会话与调试记录保存到工作区的 `.web_sessions/` 目录，采用 JSON 文件存储，方便备份和排查问题。网页端的会话运行在后台线程中，发送消息后页面会自动刷新状态。

## 解决方案

![System Architecture](images/system-architecture.svg)

S15 不再引入新机制，而是把前面各章的组件集成到同一个 harness：

```text
用户输入
  → UserPromptSubmit hooks
  → cron/background 通知注入
  → context compact
  → memory + skills + MCP 状态组装 system prompt
  → LLM
  → has tool_use block?
      否 → Stop hooks → 返回
      是 → PreToolUse hooks + permission
          → TOOL_HANDLERS / MCP handlers / background dispatch
          → PostToolUse hooks
          → tool_result / task_notification 回 messages
          → 下一轮
```

循环仍是同一个结构：调用模型，检查响应里是否出现 `tool_use` block，执行工具，再把结果追加回 `messages`。是否继续工具轮，由响应中有没有实际的 `tool_use` block 决定。

---

## 组件在循环中的位置

| 位置 | 组件 | 作用 |
|------|------|------|
| 用户输入前后 | `UserPromptSubmit` hooks | 记录、注入、审计用户输入 |
| LLM 前 | cron queue | 把定时触发的 prompt 注入 `messages` |
| LLM 前 | background notifications | 后台任务完成后以 `<task_notification>` 注入 |
| LLM 前 | compaction pipeline | 先压大输出，再裁历史，再压旧 tool_result，必要时摘要 |
| LLM 前 | memory / skills / MCP state | 组装 system prompt，让模型看到当前能力和长期上下文 |
| LLM 调用 | error recovery | 429/529 重试，`max_tokens` 升级，prompt too long 触发 reactive compact |
| 工具执行前 | `PreToolUse` hooks + permission | 拦截危险命令、写越界、破坏性 MCP 工具 |
| 工具分发 | `assemble_tool_pool` | 组装内置工具和 MCP 动态工具 |
| 工具执行时 | background dispatch | 显式标记的 bash 操作放入 daemon thread，主循环先返回占位结果 |
| 工具执行后 | `PostToolUse` hooks | 大输出告警、日志等后处理 |
| 返回循环 | tool_result | 每个 `tool_use` 对应一个 `tool_result`，再回到下一轮 |
| 本轮没有 tool_use / 停止时 | `Stop` hooks | 统计、清理、审计 |

---

## 项目结构

`code.py` 现在只保留兼容启动入口，具体运行时按职责拆分到
`coding_assistant/`：

```text
code.py                         CLI 兼容入口
coding_assistant/
  config.py                     环境、模型客户端、控制台 I/O
  memory.py                     s09 memory runtime 的可选适配层
  tasks.py                      任务图与任务绑定 worktree
  skills.py                     skill 扫描与 system prompt 组装
  tools.py                      Shell、文件、glob 与 todo 工具
  hooks.py                      生命周期 hooks 与权限策略
  subagents.py                  一次性隔离 subagent
  teams.py                      持久队友、邮箱与 plan 协议
  compaction.py                 上下文预算、transcript 与错误恢复
  background.py                 后台 Shell 任务与通知
  cron.py                       持久调度与交付确认
  mcp.py                        MCP 客户端、mock server、宿主策略元数据
  registry.py                   工具 schema、handler 与动态工具组装
  agent.py                      集成模型循环与事件唤醒
  cli.py                        交互式命令行宿主
```

依赖方向以 `agent.py` 为中心：各能力模块持有自己的状态和 handler，
`registry.py` 负责把能力暴露给模型，`agent.py` 只负责编排。新增本地工具时，
把实现放进对应能力模块，并在 `registry.py` 注册 schema/handler；新增事件源时，
参考 `background.py` 或 `cron.py`，再从 `agent.py` 注入消息循环。

memory 集成仍兼容课程中相邻的 `s09_memory/code.py`，也可以通过
`MEMORY_RUNTIME_PATH` 指定其他位置。如果找不到 memory runtime，助手会以禁用
memory 的方式启动，而不是在导入阶段直接失败。

## 运行时包含什么

### 工具与分发

内置工具池包含 27 个工具：

```text
bash, read_file, write_file, edit_file, search_text, apply_patch, glob
todo_write, task, load_skill, compact
create_task, list_tasks, get_task, claim_task, complete_task
schedule_cron, list_crons, cancel_cron
spawn_teammate, list_teammates, send_message
request_shutdown, request_plan, review_plan
create_worktree
connect_mcp
```

`search_text` 优先使用 `rg`，不可用时自动回退到 Python 搜索：

```json
{"query": "agent_loop", "glob": "*.py", "case_sensitive": false, "max_results": 50}
```

`apply_patch` 先校验全部文件和 hunk，再提交多文件修改。任意上下文不匹配时
不会写入任何文件，也可以传入 `expected_sha256` 防止修改已变化的文件：

```json
{"patches": [{"path": "app.py", "hunks": [
  {"old_text": "old_call()", "new_text": "new_call()", "expected_occurrences": 1}
]}]}
```

`assemble_tool_pool()` 每轮组装：

```text
BUILTIN_TOOLS + connected MCP tools
BUILTIN_HANDLERS + mcp__server__tool handlers
```

所以 `connect_mcp("docs")` 后，下一轮工具池里会出现 `mcp__docs__search`。

### 权限和 hooks

权限不写死在工具执行行里，而是作为 `PreToolUse` hook：

```python
blocked = trigger_hooks("PreToolUse", block)
if blocked:
    results.append(tool_result(block.id, blocked))
    continue
```

这样 permission、log、审计都可以挂在同一个 hook 点上。Lead、一次性 subagent 和队友的工具都会先经过 `PreToolUse`；允许执行的调用会在 handler 返回后触发 `PostToolUse`。

权限判断不会把 MCP server 自己写的 description 当成授权依据。文件工具越过
`WORKDIR` 始终拒绝，MCP 仍由宿主的 `allow` / `confirm` / `deny` 策略控制。

通过 `.env` 中的 `PERMISSION_MODE` 选择两档权限：

```dotenv
# 默认模式：Bash 和 confirm 类型 MCP 工具逐次请求批准
PERMISSION_MODE=request

# 完全批准：普通命令不再询问，适合可信的个人开发环境
PERMISSION_MODE=full
```

`full` 不会关闭安全底线。提权、关机、磁盘格式化、原始磁盘写入、根目录递归
删除和 fork bomb 始终直接拒绝。普通删除、Git 强制清理、越界切换目录、越界
重定向和编码后的 PowerShell 命令会回退到 `CONSOLE` 请求确认；只有普通命令
自动批准。MCP 的 `deny` 策略也始终生效。静态命令检查不是操作系统沙箱，运行
不可信代码时仍应使用容器或其他隔离环境。

### 计划与任务

S15 同时保留两层计划：

- `todo_write`：当前会话内的轻量计划，保存在内存中
- task graph：跨会话、可依赖、可认领的任务文件，写入 `.tasks/task_*.json`

前者帮助单个 Agent 不漂移；后者支撑团队协作。

两者目标相近，但实现不同：`todo_write` 整表替换当前会话清单，task record 则有稳定 ID 和单条生命周期更新。下面单独出现的 `task` 工具表示“一次性派发隔离 subagent”，不是 Task System。

### 子 agent 与团队

S15 有两种 delegation：

- `task`：一次性 subagent。独立 `messages[]`，中间过程丢弃，只返回最终摘要。
- `spawn_teammate`：持久队友线程。传入 ready `task_id` 时，运行时会在线程启动前完成认领；不传时，队友可以在 IDLE 中等待后续任务。没有 assignment 的队友不能使用文件或 Shell 工具。它按 `WORK → result → IDLE` 运行，不设固定的工具轮数上限；模型或分发失败会发出 `error`，线程清理会把未完成 assignment 释放回任务板。每次调用模型前都会先读取收件箱，因此直接消息和关机请求不会被连续的 tool-use 轮次饿死。idle 时先等待 `MessageBus` 消息，只在超时后扫描就绪 task，并以原子操作最多认领一个。

Lead 启动队友后结束当前轮次，不在模型循环里反复查询状态。队友事件进入 Lead 收件箱后，运行时会自动唤醒下一轮。

一次性 subagent 解决“上下文隔离”；持久队友解决“长期并行协作”。

### 记忆、技能和 prompt

S15 直接复用 s09 的 Memory runtime。每轮调用模型前，它读取 `.memory/MEMORY.md` 目录，根据当前请求选择相关记录，再把选中的正文交给 `assemble_system_prompt(context)`。本轮结束后，`extract_memories()` 提取可跨会话使用的信息；有新增记录时再运行 `consolidate_memories()`。

同一份 system prompt 还会加入身份、工具说明、workspace、skills catalog 和已连接的 MCP server。技能只放目录，完整内容通过 `load_skill(name)` 按需加载。

### 压缩和恢复

LLM 前先跑压缩管线：

```text
tool_result_budget → snip_compact → micro_compact → compact_history
```

调用模型时再包一层恢复：

- 429：指数退避重试
- 529：指数退避，连续失败可切 fallback model
- `max_tokens`：先提高 max_tokens，再要求 continuation
- prompt too long：reactive compact 后重试

### 后台和 cron

bash 调用设置 `run_in_background=true` 后，主循环不再等待命令结束，而是先返回占位结果：

```text
should_run_background → start_background_task → placeholder tool_result
后台完成 → task_notification → 下一轮注入 messages
```

只有显式标记的 bash 调用会进入后台路径。命令非零退出或 worker 抛出异常时会发出 `failed` 通知。每条 Shell 命令都在独立进程组中运行；命令结束，或 Agent 经正常路径、`SIGTERM` 退出时，运行时会停止原进程组。另建 session 的进程可以离开这个进程组。

cron 调度器独立 daemon thread 每秒检查一次。durable 的一次性任务会先持久化为 `pending_delivery`，再进入队列，并保留到包含该 prompt 的模型调用成功；调用失败会放回队列，重启后也会再次入队，因此交付语义是至少一次。CLI 同时监听 `cron_queue`、Lead 收件箱和已经结束的后台任务，任一事件都能自动唤醒一轮 Agent。

### worktree 与 MCP

从 s13 继承的任务级 worktree 机制负责管理任务工作目录：

- pending 且未被认领的 task 可以留在主工作区，也可以通过 `create_worktree(name, task_id)` 绑定独立分支和目录
- 创建前会校验 task、名称、路径、分支和 Git registry；Git 命令失败后还会核对 registry 和分支状态，任何部分创建的 checkout 都保持未绑定并保留供人工恢复
- idle 队友以原子操作认领一个就绪 task，assignment 同时记录 `task_id` 和有效 `cwd`
- Lead 也可以把 ready `task_id` 直接传给 `spawn_teammate`，认领成功后才启动线程
- 队友所有文件工具都使用该 `cwd`；只有 task owner 能完成任务，assignment 会保留到当前模型轮次结束
- 移除保留在宿主侧的 `remove_worktree()` 函数中，模型不能调用。用户或宿主先检查任务所有权、assignment lease、后台工作和 Git 状态；破坏性移除需要另行取得用户确认

worktree 只改变工具的默认工作目录，用于分离 working copy，并不是安全沙箱。进程组清理也无法约束另建 session 的进程，因此删除保留为宿主操作。

认领或释放 task 会改变 assignment version，使旧的 plan approval 失效；普通 `send_message` 只传递消息，不会改变 task identity 或 plan 状态。

MCP 负责外部能力：

- `connect_mcp(name)` 连接 mock server
- `assemble_tool_pool()` 把 MCP 工具组装进工具池，并拒绝规范化后的名称冲突
- 工具名统一为 `mcp__server__tool`

---

## 相对 s14 的变化

| 范围 | s14 MCP | s15 Integrated Harness |
|------|---------|-------------------------|
| 内置工具 | 6 个 | 25 个 |
| 外部工具 | 已连接的 MCP 工具 | 沿用同一套动态 MCP 路径和宿主策略 |
| 本地机制 | S04 工具、hooks、权限和 MCP | todo、subagent、skills、compaction、memory、task graph、后台 bash、cron、teams 和 worktrees |
| 事件来源 | 用户输入和工具结果 | 用户输入、工具结果、cron prompt、后台通知和 team events |

---

## 试一下

```sh
cd learn-claude-code
python s15_integrated_harness/code.py
```

可以试：

1. `检查这个仓库，告诉我哪些 Python 文件最重要。`
2. `从已连接的文档中查一下 agent loop 的相关说明。`
3. `请在独立的 worktree 中并行重构认证模块和登录页，修改前先把各自的计划给我看。`
4. `3 分钟后提醒我开会。`
5. `在后台安装依赖，同时继续阅读 README.md。`

观察重点：

- 工具调用前是否经过 hooks/permission
- `connect_mcp` 后下一轮是否出现 MCP 工具
- 设置 `run_in_background=true` 的 bash 调用是否返回 background placeholder
- 到点是不是自动提醒开会
- 队友是否提交 plan，并在 approval 前暂停
- idle 队友是否只原子认领一个就绪 task
- 队友所有文件工具是否都切换到已认领 task 的 `cwd`
- 完成任务后是否在本轮剩余工具调用中保持 task `cwd`，并在 IDLE 时释放

---


## Token 与 Prompt Cache 优化

运行时现在通过统一的 LLM 调用层记录 token 使用量，并优先使用 Anthropic Prompt Caching：

- 固定 system 指令和稳定工具 schema 形成缓存前缀；动态 memory、teammate 状态和工具结果位于缓存断点之后；
- 普通编码请求只暴露核心工具，任务、Cron、Team 和 MCP 工具按本地关键词按需加入；
- 上下文达到 70%/85%/95% 预算时，依次压缩旧工具结果、裁剪历史和生成状态摘要；
- 指标写入 `.token_usage/usage-YYYY-MM-DD.jsonl`，浏览器 Trace 展示会话累计输入、输出、cache read/create 和命中率；
- 兼容网关不支持 `cache_control` 时会自动降级为普通 Messages API 请求。

可通过 `.env` 调整 `PROMPT_CACHE_ENABLED`、`PROMPT_CACHE_TTL`、三个上下文阈值、`KEEP_RECENT_TOOL_RESULTS` 和 `TRACE_FULL_PAYLOAD`。默认 Trace 只保存 prompt hash、长度、工具名称及 token 指标，避免重复保存完整模型输入。
