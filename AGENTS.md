# AGENTS.md - 项目coding agent配置

## 项目概览

本项目实用 Python3.14 复现一个轻量版的 OpenClaw 框架
**参考项目：** [pickle-bot](https://github.com/czl9707/pickle-bot)

## 项目结构（最终目标版）

- 工作空间（workspace/） 是纯文件驱动的配置层，所有行为都从这里声明——config.json 决定用哪个 LLM 和渠道，AGENT.md 定义 Agent 人格，MEMORY.md 持久化跨会话记忆，skills/ 里放 SKILL.md 教会 Agent 新技能。这是 OpenClaw 哲学的核心：行为即文件。
- Agent 核心 是 agentic loop 的实现，负责组装多层 prompt、调用工具、处理工具结果、压缩上下文（compaction）、在压缩前把重要信息写入记忆，以及响应 slash commands。
- Gateway 服务层 把 Agent 从 CLI 解放出来，变成一个常驻后台的事件驱动服务——Event Bus 负责路由，WebSocket 让外部程序接入，Cron Heartbeat 让 Agent 能主动定时工作，Config Hot Reload 改配置不用重启。
- Channel 层：EventSource、Channel、CliChannel、TelegramChannel、DiscordChannel、ChannelWorker、DeliveryWorker、outbox retry 持久投递。
- 工具层 是水平共享的能力：搜索、抓页面、读写文件、执行 shell 命令，以及最终阶段加入的 Agent Dispatch（派遣子 Agent 干活）。
- 多 Agent 编排 在最顶层，Router Agent 根据任务类型把请求分发给专门的 Agent，实现分工协作。

### 路线

1. 让智能体学会聊天、用工具、加载技能、保存对话、上网搜索。（已完成）
2. 换成事件驱动架构，配置热重载，支持多平台接入。（已完成）
3. WebSocket入口，让程序也能像 Telegram/Discord 一样给 agent 发消息。（已完成）
4. 定时任务、智能路由、智能体主动发送通信、多智能体协作。（已完成）
5. 并发控制和长期记忆。（正在进行：并发控制）

## 代码规范

- 修改代码前查看 `code-review-skill` 保持规范
- 写代码不要只关注功能实现，还要考虑架构、可维护性，注意解耦
- 读取文件用绝对路径，防止沙盒造成的读取失败问题；utf-8 编码防止中文乱码
- 善用第一性原理，代码禁止禁止禁止过度防御！！！

## 当前目标

quclaw业务场景：试用期员工主动离职流程
详细要求查看文档：[试用期主动离职](E:\code\agent\Quclaw\current_goal.md)



## 后面再做的（不用管）

1. CLI 超时不会取消 AgentWorker 里的实际执行。后续计划将 llm 改成 进度事件/流式响应 -> 再做取消/超时控制
2. memory 修改

```md
| 方法 | 描述 |
|------|------|
| **专用智能体**（本实现）| 通过调度访问的记忆智能体 |
| **内置工具**| 主智能体直接带记忆工具 |
| **基于技能**| 用 grep 等 CLI 工具 |
| **向量数据库**| embedding + 语义搜索 |
```
