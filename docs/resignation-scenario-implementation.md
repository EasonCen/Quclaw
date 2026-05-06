# 试用期主动离职场景实施记录

本文记录“试用期员工主动离职场景最小流程配置题”的实施过程、架构思考和当前落地状态。它用于承接 `current_goal.md` 与 `resignation-scenario-spec-v4.md`，后续每个阶段继续在这里补充实现决策。

## 目标背景

目标不是写一个固定脚本，而是在 Quclaw 现有事件驱动架构中落地一个可演示的多角色业务流程：

- 员工窗口：提交试用期主动离职意向。
- HR 窗口：确认申请、最后工作时间、最终结案签核。
- TL 窗口：完成交接摘要。
- Ops 窗口：完成权限与资产回收。
- Admin 窗口：后台管理视角，只读查看状态和审计。

因此实现重点有两个：

- 业务流程必须有明确状态机，不能靠 Agent 文案“假装推进”。
- 多窗口权限必须由可信 source 隔离，不能由用户或 LLM 在 payload 里自称角色。

## 架构思考

### 1. Orchestrator 不放业务细节

早期讨论中明确了一个边界：`ScenarioOrchestrator` 只做编排，不承载复杂业务逻辑。

当前拆分如下：

| 模块 | 职责 |
| --- | --- |
| `model.py` | 定义 case、phase、status、step、audit、notification、result 等数据模型 |
| `state.py` | 使用 `python-statemachine` 定义主状态机合法流转 |
| `rules.py` | 角色解析、权限判断、payload 校验、关闭条件判断 |
| `effects.py` | 修改 case 字段、追加 audit、生成通知、构造 archive payload |
| `views.py` | 按 employee/hr/tl/ops/admin 生成角色视图 |
| `store.py` | 维护 `cases/` 和 pending registry 的私有文件存储 |
| `orchestrator.py` | 接收 action，串联 store/rules/state/effects/views |

这样做的原因是：后续接 Tool、EventBus、cron、archive-agent 时，不需要把基础业务判断塞回入口文件。

### 2. 主状态机只表达主流程

主状态只保留四个 phase：

```text
awaiting_hr_confirm
  -> handover_and_recovery
  -> awaiting_hr_signoff
  -> closed
```

`pending_intent` 不进入主状态机，因为它发生在 case 创建之前。

TL/Ops 并行阶段没有拆成两个主状态，而是用 `step_3.tl_done` 和 `step_3.ops_done` 表达并行完成情况：

```text
TL 先完成：phase 仍是 handover_and_recovery
Ops 先完成：phase 仍是 handover_and_recovery
两边都完成：phase 进入 awaiting_hr_signoff
```

超时升级也不作为主状态。升级只写入：

```text
case.escalated
case.escalations
step_3.tl_reminder_count / ops_reminder_count
```

这样避免 `case.status = escalated` 与主流程 phase 打架。

### 3. 权限隔离由 source 决定

Scenario 权限不信任 payload，只信任 `session.source`。

角色 source 形如：

```text
platform-telegram:employee/<chat_id>
platform-telegram:hr/<chat_id>
platform-telegram:tl/<chat_id>
platform-telegram:ops/<chat_id>
platform-telegram:admin/<chat_id>
cron:resignation-monitor
```

`rules.role_from_source()` 根据 source 解析角色，然后每个 action 再用 `require_role()` 校验权限。

这带来几个明确效果：

- employee 不能执行 `hr_confirm`。
- admin 不能执行任何 `complete_task`。
- default Telegram bot 没有 scenario role，不能进入 Scenario 流程。
- cron 只作为 system source，用于后续 `scan_timeouts`。

### 4. Tool 层是运行时边界

`ScenarioOrchestrator` 不依赖 EventBus，也不直接发 Telegram 消息。

`scenario_engine` 工具负责把 Agent 世界接入 Scenario 世界：

```text
Agent
  -> scenario_engine(action, payload)
  -> 注入 session.source
  -> ScenarioOrchestrator.handle(...)
  -> 返回 scenario result
```

工具只暴露 `action` 和 `payload`，不暴露 `source`。

为了减少工具暴露面，`scenario_engine` 不是注册给所有 Agent，而是只在 source 能解析为 Scenario 角色时才注册：

```text
employee/hr/tl/ops/admin source -> 注册 scenario_engine
cron source                    -> 注册 scenario_engine
default Telegram / CLI          -> 不注册 scenario_engine
```

### 5. 通知投递放在 Tool 层

ScenarioEngine 业务层只生成 `NotificationIntent(target_role, content)`，真正投递在 `scenario_tool.py` 中完成。

原因：

- Tool 层能访问 `session.source`、`session.session_id` 和 `context.eventbus`。
- Orchestrator 保持纯业务编排，不绑定运行时基础设施。
- 投递失败不应该回滚已经成功保存的 case 状态。

投递链路：

```text
ScenarioResult.notifications
  -> scenario_tool 解析 target_role 到目标 source
  -> EventBus.publish(OutboundEvent)
  -> DeliveryWorker
  -> TelegramChannel.reply
  -> 目标角色 Bot
```

工具返回外包为：

```json
{
  "scenario": {
    "ok": true,
    "code": "hr_confirm_completed"
  },
  "delivery": {
    "published": 2,
    "skipped": [],
    "errors": []
  }
}
```

`scenario` 是业务结果，`delivery` 是运行时通知投递结果。

## 已落地功能

### Phase 1：Telegram 多 Bot 窗口

已支持多 Telegram bot 配置：

```text
default
employee
hr
tl
ops
admin
```

角色 bot 会生成带 bot_key 的 source，并通过 routing 进入对应 Agent。

default bot 保持普通 Quclaw 行为，不进入 Scenario 角色流程。

### Phase 2：Scenario 主状态机

已实现：

- `init_case`
- `complete_task/hr_confirm`
- `complete_task/tl_done`
- `complete_task/ops_done`
- `complete_task/hr_sign`
- `scan_timeouts`
- `get_status`
- `get_audit_log`
- pending intent 相关 action
- source registry

状态机覆盖：

```text
awaiting_hr_confirm
handover_and_recovery
awaiting_hr_signoff
closed
```

### Phase 3：ScenarioEngineTool

已实现统一工具：

```text
scenario_engine
```

工具参数：

```json
{
  "action": "complete_task",
  "payload": {
    "task_type": "hr_confirm",
    "last_working_day": "2026-05-20"
  }
}
```

工具内部固定使用 `session.source`，防止 payload 伪造身份。

### Phase 4：EventBus 通知投递

已实现：

- 工具调用前自动注册当前角色 source。
- active case 存在时同步写入 `case.role_sources`。
- notification 根据 target role 解析目标 source。
- 找不到目标窗口时写入 `delivery.skipped`。
- EventBus publish 失败时写入 `delivery.errors`。
- publish 成功时交给现有 DeliveryWorker 和 Channel 发送。

通知投递依赖目标角色 source 已注册。角色窗口可以通过 `register_source`、`get_status` 或任意一次 `scenario_engine` 调用自动进入通讯录；如果 HR/TL/Ops/Admin 从未调用过工具，系统不知道对应 chat_id，会把通知记入 `delivery.skipped`。

当前通知采用单据格式。员工创建 case 后，HR 会收到申请单号、员工 source 和创建时间，创建时间格式为 `YYYY/MM/DD:HH:MM:SS`。HR 确认最后工作时间后，TL/Ops 会收到申请单号、员工 source、创建时间、最后工作时间、HR 确认时间和各自任务说明；最后工作时间格式为 `YYYY/MM/DD:HH:MM`。

典型场景：

```text
HR confirm
  -> 通知 TL/Ops

TL + Ops 均完成
  -> 通知 HR 签核

scan_timeouts
  -> 通知 TL/Ops
  -> 第 3 次后升级通知 HR/Admin
```

### Phase 5：Cron 定时扫描

已新增 `resignation-monitor` cron 定义：

```text
default_workspace/crons/resignation-monitor/CRON.md
```

该任务每 1 分钟运行一次，派发给 `admin` Agent，并以 `cron:resignation-monitor` 作为 source 调用：

```json
{"action":"scan_timeouts","payload":{}}
```

权限仍由 source 决定：cron source 会被解析为 `system`，因此能执行 `scan_timeouts`，但不会获得 HR/TL/Ops 的业务任务权限。

当前 v1 没有 reminder cooldown；只要 case 处于 `handover_and_recovery` 且 deadline 已过，每次 cron 命中都会增加对应 reminder count。由于 CronLoader 要求最小粒度 1 分钟，当前 schedule 使用：

```text
*/1 * * * *
```

## 当前数据流

完整流程如下：

```text
Telegram role bot message
  -> ChannelWorker
  -> InboundEvent
  -> AgentWorker
  -> role Agent
  -> scenario_engine tool
  -> auto register session.source
  -> ScenarioOrchestrator.handle
  -> rules 权限校验
  -> state machine 流转
  -> effects 修改 case / 生成 notifications
  -> store 保存 case
  -> scenario_tool 发布 OutboundEvent
  -> EventBus
  -> DeliveryWorker
  -> TelegramChannel.reply
```

后台超时扫描链路如下：

```text
resignation-monitor CRON.md
  -> CronWorker
  -> DispatchEvent(source=cron:resignation-monitor, target_agent_id=admin)
  -> Admin Agent
  -> scenario_engine scan_timeouts
  -> ScenarioOrchestrator
  -> EventBus notification delivery
```

## 测试覆盖

当前测试覆盖以下方面：

- `init_case` 后 phase 为 `awaiting_hr_confirm`。
- `hr_confirm` 后 phase 为 `handover_and_recovery`。
- TL/Ops 任意顺序完成后进入 `awaiting_hr_signoff`。
- HR sign 后 phase 和 status 均为 closed。
- timeout 不改变 phase，只增加提醒/升级标记。
- employee 不能执行 HR 任务。
- admin 不能执行任何 complete task。
- admin view 包含 audit，HR view 不包含完整 audit。
- `scenario_engine` 只用 `session.source`，不信 payload source。
- 只有 scenario role source 才注册 `scenario_engine`。
- default Telegram / CLI 看不到 scenario tool。
- HR confirm 后能投递 TL/Ops。
- 未注册目标窗口时写入 skipped。
- publish 失败不回滚 case。
- cron timeout scan 能投递给已注册角色。
- CronLoader 能发现 `resignation-monitor`。
- CronWorker 能把 `resignation-monitor` 派发成 `DispatchEvent`。

当前验证结果：

```text
30 passed
```

## 当前未完成边界

以下内容还未进入当前实现：

- archive-agent 自动归档。
- file tools 对 `cases/` 的硬隔离。
- 更复杂的多 case 管理。
- timeout reminder cooldown。
- production 级 source 注册清理和过期策略。
- Telegram 真实端到端人工验收。

## 后续建议

下一步建议处理 timeout reminder cooldown，避免每分钟扫描都增加提醒次数。

之后再接 archive-agent：

```text
hr_sign
  -> archive_payload
  -> archive-agent / admin report
```

最后再处理工具层硬隔离，限制通用 file tools 直接访问 `cases/`，让 Scenario 状态只能通过 `scenario_engine` 修改。
