# Quclaw

Quclaw 是一个用 Python 复现的轻量版 OpenClaw 风格 Agent 框架。项目核心理念是“行为即文件”：模型配置、Agent 人格、技能、定时任务、长期记忆和渠道接入都由 workspace 中的文件声明。

## 功能概览

- Agent 对话循环、工具调用、会话持久化
- 基于 workspace 的文件驱动配置
- 多层 Prompt：`AGENT.md`、`SOUL.md`、`BOOTSTRAP.md`、workspace `AGENTS.md`
- 内置文件读写、编辑、Shell 工具
- Skill 加载机制
- EventBus 驱动的后台服务
- CLI、WebSocket、Telegram、Discord 接入
- Cron 定时任务和 Heartbeat 后台检查
- Cron 场景下的主动消息发送 `post_message`
- 多 Agent 协作 `subagent_dispatch`
- 按 Agent 的并发控制 `max_concurrency`
- 通过 Cookie 记忆 Agent 管理长期记忆

## 架构图

### 运行时架构

![Quclaw 运行时架构](docs/runtime-architecture-cn.svg)

### 事件流

![Quclaw 事件流](docs/event-flow-cn.svg)

### 长期记忆流程

![Quclaw 长期记忆流程](docs/memory-agent-flow-cn.svg)

## 环境要求

- Python 3.14+
- 推荐使用 `uv` 管理依赖

## 快速开始

安装依赖：

```bash
uv sync
```

准备配置文件：

```powershell
Copy-Item default_workspace/config.example.yaml default_workspace/config.user.yaml
```

然后在 `default_workspace/config.user.yaml` 中配置 LLM provider、model 和 API key。

启动 CLI 对话：

```bash
uv run quclaw --workspace default_workspace chat
```

启动后台服务：

```bash
uv run quclaw --workspace default_workspace server
```

后台服务会加载 EventBus、渠道 Worker、DeliveryWorker、Cron、Heartbeat、WebSocket 等组件。

## Workspace 结构

```text
default_workspace/
├── config.user.yaml       # 用户配置：LLM、渠道、路径、路由等
├── config.runtime.yaml    # 运行时状态，程序自动维护
├── AGENTS.md              # workspace 内 Agent 协作规则
├── BOOTSTRAP.md           # workspace 通用上下文
├── HEARTBEAT.md           # 静默后台检查清单
├── agents/
│   └── <agent_id>/
│       ├── AGENT.md       # Agent 身份、配置、指令
│       └── SOUL.md        # 可选人格层
├── skills/
│   └── <skill_id>/SKILL.md
├── crons/
│   └── <cron_id>/CRON.md
└── memories/
    ├── topics/
    ├── projects/
    └── daily-notes/
```

## Agent 配置

Agent 定义在 `agents/<agent_id>/AGENT.md` 的 YAML frontmatter 中：

```yaml
---
name: Qu
description: Default user-facing assistant
allow_skills: true
max_concurrency: 3
llm:
  temperature: 0.7
  max_tokens: 4096
---
```

`max_concurrency` 用来限制同一种 Agent 同时运行的任务数量。达到限制时，新任务会等待。

## 长期记忆

长期记忆由 `cookie` Agent 负责。主 Agent 应通过 `subagent_dispatch` 委托 Cookie 存取记忆，而不是直接读写记忆文件。

记忆目录由 `memories_path` 配置，默认是：

```yaml
memories_path: memories
```

默认记忆结构：

```text
memories/
├── topics/       # 长期事实、偏好、身份信息
├── projects/     # 项目上下文
└── daily-notes/  # 每日记录
```

## 测试

运行完整测试：

```bash
uv run python -m pytest -q tests
```

## 代码结构

```text
src/
├── cli/       # Typer CLI 入口
├── channel/   # 平台渠道
├── core/      # Agent、事件、配置、历史、Prompt、路由
├── provider/  # LLM provider 适配
├── server/    # 事件驱动 Worker
├── tools/     # 共享工具
└── utils/     # 配置和定义加载工具
```

## 当前状态

Quclaw 已覆盖轻量 Agent 框架的主要链路：对话、工具、技能、事件总线、渠道接入、持久投递、Cron、Heartbeat、主动消息、多 Agent 调度、并发控制和基于文件的长期记忆。
