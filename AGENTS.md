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
4. 定时任务、智能路由、多智能体协作。（正在进行：当前先做 source-based multi-agent routing）
5. 并发控制和长期记忆。

## 代码规范

- 修改代码前查看 `code-review-skill` 保持规范
- 写代码不要只关注功能实现，还要考虑架构、可维护性，注意解耦
- 读取文件用绝对路径，防止沙盒造成的读取失败问题；utf-8 编码防止中文乱码
- 善用第一性原理，不要自作聪明画蛇添足，只做要求完成的事

## 当前目标

WebSocket 的基础上，加一个确定性的多 Agent 基础能力：根据消息来源 source，把新会话分配给不同 agent。
> 当前阶段的 multi-agent 是 source-based routing，不是内容理解型 Router Agent，也不是 Agent 之间互相派活。

当前语义：

- **route 决定新会话归属**：`/route <source_pattern> <agent_id>` 表示匹配这个 source pattern 的新会话应该由哪个 agent 接手。
- **session 归属不可变**：一个 session 创建后会固化为 `session_id -> agent_id`；后续恢复这个 session 时继续使用历史里的 agent，不中途换人格。
- **source cache 只是续接指针**：runtime 里的 `source -> session_id` 只表示这个来源默认续接哪个会话；它不是 agent 归属本身。
- **兜底使用默认智能体**：没有任何 binding 匹配时，使用 `default_agent`。

这意味着当前阶段的链路是：

```text
source
  -> source session cache 命中：恢复旧 session，沿用旧 agent
  -> source session cache 未命中：RoutingTable.resolve(source)
  -> 创建新 session
  -> session 固化到某个 agent
  -> AgentWorker 执行这个 session 对应的 agent
```

`/route` 的语义不是“迁移旧会话到新 agent”，而是“改变匹配 source 后续新会话的默认归属”。如果要让已有 source 立刻按新 route 生效，应重置匹配 source 的 `source -> session_id` cache，让下一条消息创建一个干净的新 session；旧 session 历史仍然保留。

例子：

```yaml
routing:
  bindings:
    - value: platform-ws:coder/.*
      agent: coder
    - value: platform-telegram:.*
      agent: Qu
```

```text
platform-ws:coder/task-1  -> 新 session -> coder
platform-telegram:123     -> 新 session -> Qu
没匹配上                  -> 新 session -> default_agent
```

当前阶段不做：

- 不根据消息内容自动判断任务类型。
- 不让多个 agent 对同一条消息辩论。
- 不让 agent A 主动 dispatch agent B。
- 不把已有 session 的历史强行迁移到另一个 agent。

### 关键组件

- **AgentLoader** - 发现并加载多个智能体定义
- **RoutingTable** - 正则匹配 + 分层优先级，把消息源路由到智能体
- **Binding** - 源模式 + 智能体映射，自动计算优先级
- **Commands** - `/route`、`/bindings`、`/agents` 管理路由
- **Source Session Cache** - runtime 中的 `source -> session_id` 续接指针

- **分层路由**：从最具体的规则开始匹配。
- **兜底**：没匹配上就用默认智能体。



## 后面再做的（不用管）

CLI 超时不会取消 AgentWorker 里的实际执行。
后续计划将 llm 改成 进度事件/流式响应 -> 再做取消/超时控制
