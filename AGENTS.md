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
4. 定时任务、智能路由、多智能体协作。（正在进行：多层提示）
5. 并发控制和长期记忆。

## 代码规范

- 修改代码前查看 `code-review-skill` 保持规范
- 写代码不要只关注功能实现，还要考虑架构、可维护性，注意解耦
- 读取文件用绝对路径，防止沙盒造成的读取失败问题；utf-8 编码防止中文乱码
- 善用第一性原理，不要自作聪明画蛇添足，只做要求完成的事

## 当前目标

多层提示
> 更多上下文，更多上下文，更多上下文。

系统提示分多层组装：身份、性格、工作区上下文、运行时信息。

五层提示词：
第1层：身份层
来自 AGENT.md。比如 Qu 的身份、能力、行为准则。
default_workspace/agents/Qu/AGENT.md

它回答的是：
“你是谁？你能做什么？基本行为边界是什么？”

第 2 层：性格层
来自 SOUL.md。
default_workspace/agents/Qu/SOUL.md

这一层回答的是：
“你说话是什么风格？你像一个怎样的助手？”

第 3 层：工作区上下文
来自：

default_workspace/BOOTSTRAP.md
default_workspace/AGENTS.md

PromptBuilder 会读它们：

prompt_builder.py

这层告诉模型：

“这个 workspace 有哪些目录？agents、skills、crons、memories 在哪里？什么时候该调度别的 agent？”

第 4 层：运行时上下文
这层回答的是：
“现在是谁在运行？当前时间是什么？”

第 5 层：渠道提示
这里根据事件来源生成
例如：

```python
if source.is_cron:
    return "You are running as a background cron job..."
if source.is_agent:
    return "You are running as a dispatched subagent..."
elif source.is_platform:
    return f"You are responding via {source.platform_name}."
```

这层很重要。因为同一个 agent 在不同场景下行为应该不同：

CLI / Telegram / WebSocket：直接回复用户。
Cron：后台任务，不一定直接发给用户。
Subagent：结果要回传给主 agent，而不是像聊天一样闲聊。

---

这次的目标是在把 prompt 从一个大坨文本，升级成一个可维护的系统：

AGENT.md 管身份和能力。
SOUL.md 管人格和语气。
BOOTSTRAP.md 管 workspace 规则。
AGENTS.md 管多 agent 协作规则。
runtime layer 管当前时间、当前 agent、当前渠道。

这样以后想加“记忆层”“用户偏好层”“项目上下文层”，就不用把所有东西塞进 AGENT.md。直接在 PromptBuilder.build() 里加一层就行。
架构可以按需加层。比如加个**记忆层**，注入历史对话的相关内容。

### 关键组件

- **AgentDef** - `soul_md` 扩展
- **PromptBuilder** - 将所有提示层组装成最终系统提示

## 该阶段完成后的下一步

智能体主动发送通信
  
## 后面再做的（不用管）

CLI 超时不会取消 AgentWorker 里的实际执行。
后续计划将 llm 改成 进度事件/流式响应 -> 再做取消/超时控制
