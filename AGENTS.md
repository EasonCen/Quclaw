# AGENTS.md - 项目coding agent配置

## 项目概览

本项目实用 Python3.14 复现一个轻量版的 OpenClaw 框架
**参考项目：** [pickle-bot](https://github.com/czl9707/pickle-bot)


## 项目结构（最终目标版）

- 工作空间（workspace/） 是纯文件驱动的配置层，所有行为都从这里声明——config.yaml 决定用哪个 LLM 和渠道，AGENT.md 定义 Agent 人格，MEMORY.md 持久化跨会话记忆，skills/ 里放 SKILL.md 教会 Agent 新技能。这是 OpenClaw 哲学的核心：行为即文件。
- Agent 核心 是 agentic loop 的实现，负责组装多层 prompt、调用工具、处理工具结果、压缩上下文（compaction）、在压缩前把重要信息写入记忆，以及响应 slash commands。
- Gateway 服务层 把 Agent 从 CLI 解放出来，变成一个常驻后台的事件驱动服务——Event Bus 负责路由，WebSocket 让外部程序接入，Cron Heartbeat 让 Agent 能主动定时工作，Config Hot Reload 改配置不用重启。
- Channel 层：EventSource、Channel、CliChannel、TelegramChannel、DiscordChannel、ChannelWorker、DeliveryWorker、outbox retry 持久投递。
- 工具层 是水平共享的能力：搜索、抓页面、读写文件、执行 shell 命令，以及最终阶段加入的 Agent Dispatch（派遣子 Agent 干活）。
- 多 Agent 编排 在最顶层，Router Agent 根据任务类型把请求分发给专门的 Agent，实现分工协作。

### 路线

1. 让智能体学会聊天、用工具、加载技能、保存对话、上网搜索。（已完成）
2. 换成事件驱动架构，配置热重载，支持多平台接入。（已完成）
3. WebSocket入口，让程序也能像 Telegram/Discord 一样给 agent 发消息。（已完成）
4. 定时任务、智能路由、多智能体协作。（正在进行：定时任务）
5. 并发控制和长期记忆。

## 代码规范

- 修改代码前查看 `code-review-skill` 保持规范
- 写代码不要只关注功能实现，还要考虑架构、可维护性，注意解耦
- 读取文件用绝对路径，防止沙盒造成的读取失败问题；utf-8 编码防止中文乱码
- 善用第一性原理，不要自作聪明画蛇添足，只做要求完成的事

## 当前目标

Cron + Heartbeat
> Agent 在你睡觉时候工作

定时任务——智能体按 cron 表达式自动跑。
以前只有用户发消息才会触发 agent；现在开始，系统自己也可以按时间触发 agent。

- Cron 操作功能使用 **SKILL 系统**实现，而不是注册专用工具，这避免了工具注册表的膨胀。

### 关键组件

CronDef
CronLoader
CronWorker
CronEventSource
DispatchEvent
DispatchResultEvent

- **CRON.md & CronDef** - Cron 任务定义
- **CronWorker** - 每分钟检查待执行任务的后台工作器
- **DispatchEvent** - 内部任务调度的事件类型
- **DispatchResultEvent** - 调度任务返回的结果事件
- **Cron-Ops Skill** - 用于创建、列出和删除定时 cron 任务的技能（实现为技能以避免额外的工具注册）

### 需要注意

不用 `InboundEvent`，而是 `DispatchEvent`

以前用户消息是：`InboundEvent`
意思是：外部用户/平台输入。

cron 不是用户输入，它是系统内部调度任务，所以用了：`DispatchEvent`
它表示：
系统内部派发给某个 agent 的任务`AgentWorker` 也因此改了：

```python
self.context.eventbus.subscribe(InboundEvent, self.dispatch_event)
self.context.eventbus.subscribe(DispatchEvent, self.dispatch_event)
```

于是 AgentWorker 可以处理两类任务：

- 用户消息 `InboundEvent`
- 后台任务 `DispatchEvent`

### CRON vs HEARTBEAT

- **HEARTBEAT**：只有一个，固定间隔跑，不管几点
- **CRON**：可以有多个，按 cron 表达式跑，精确到分钟

## 后面再做的（不用管）

CLI 超时不会取消 AgentWorker 里的实际执行。
后续计划将 llm 改成 进度事件/流式响应 -> 再做取消/超时控制
