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
3. WebSocket入口，让程序也能像 Telegram/Discord 一样给 agent 发消息。（当前阶段）
4. 定时任务、智能路由、多智能体协作。
5. 并发控制和长期记忆。

## 代码规范

- 修改代码前查看 `code-review-skill` 保持规范
- 写代码不要只关注功能实现，还要考虑架构、可维护性，注意解耦
- 读取文件用绝对路径，防止沙盒造成的读取失败问题；utf-8 编码防止中文乱码
- 善用第一性原理，不要自作聪明画蛇添足，只做要求完成的事

## 当前目标

WebSocket
> 想要以编程方式与智能体交互？

开个 WebSocket 接口，方便程序调用。

- WebSocket 只是新的输入/输出适配器。核心业务仍然是 EventBus + Worker + Event。

### 关键组件

- **WebSocketWorker** - 管理 WebSocket 连接并广播事件
  - WebSocketWorker 负责托管 FastAPI/uvicorn WebSocket 服务，同时订阅 EventBus，把匹配的 InboundEvent/OutboundEvent 推送给连接中的客户端。
  - WebSocket 不走 DeliveryWorker 的 outbox retry；它是在线连接适配器，断线后的重放/离线投递后续再做。
  - WebSocket 客户端只能提交 `source` 和 `content`；`session_id` 由服务端通过 `source -> session_id` runtime mapping 查找或创建。
- **WebSocket Handle** - 具有 WebSocket 端点的 Web 服务器

## 后面再做的（不用管）

CLI 超时不会取消 AgentWorker 里的实际执行。
后续计划将 llm 改成 进度事件/流式响应 -> 再做取消/超时控制
