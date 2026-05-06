# Scenario Engine 流程说明

本目录实现“试用期员工主动离职场景”的核心流程引擎。当前版本负责 Scenario 内部状态机、权限校验、业务字段变更、视图生成和文件存储，并通过 `scenario_engine` 工具供 Agent 调用。`scenario_engine` 生成的通知会由工具层投递到 EventBus；HR 结案后工具层会自动派发归档任务给 Admin Agent，由 Admin Agent 读取完整归档视图，并通过受控 `scenario_notify` 工具向 employee/hr 投递脱敏摘要。

## 核心边界

Scenario Engine 接收统一 action：

```text
handle(action, payload, source)
```

其中：

- `action` 表示外部想做什么，例如 `init_case`、`complete_task`、`get_status`。
- `payload` 是动作参数，例如 `last_working_day`、`tl_summary`。
- `source` 是可信事件来源，例如 `platform-telegram:hr/123`、`cron:resignation-monitor`。

引擎只相信 `source` 解析出的角色，不相信用户自己在 payload 里声明的角色。

`source` 只用于权限校验、会话路由和审计来源，不再作为业务展示身份。员工、HR、TL、Ops 的姓名和编号必须由对应动作的 payload 显式提供，并以 `姓名(ID: 编号)` 写入 case。Admin 全局视图会展示各角色负责人汇总。

## 主流程状态

主状态由 `case.phase` 表示，只描述流程当前处于哪个阶段：

```text
awaiting_hr_confirm
  -> handover_and_recovery
  -> awaiting_hr_signoff
  -> closed
```

含义如下：

| Phase | 含义 |
| --- | --- |
| `awaiting_hr_confirm` | 已创建 case，等待 HR 确认申请与最后工作时间 |
| `handover_and_recovery` | HR 已确认，TL 交接与 Ops 回收并行处理中 |
| `awaiting_hr_signoff` | TL/Ops 均完成，等待 HR 最终签核 |
| `closed` | HR 已签核，流程关闭 |

`pending_intent` 不进入主状态机，它是 case 创建前的临时确认状态。

## 时间字段

引擎面向中国大陆用户，业务时钟统一使用 UTC+8，也就是 `Asia/Shanghai`。内部需要做 deadline 计算时使用 timezone-aware `datetime`，但所有写入 case、pending registry 和 audit log 的时间字段都统一保存为 Unix timestamp 秒级整数，例如：

```text
created_at
completed_at
deadline
detected_at
expires_at
audit_log[].ts
```

Unix timestamp 本身不携带时区。后续展示给用户时，外层 UI 或 Agent 回复应按 `Asia/Shanghai` 转回本地时间。

## 状态流转

```text
员工表达离职意向
  -> set_pending_intent
  -> 等待员工二次确认和姓名/编号

员工二次确认且 pending 未过期
  -> init_case
  -> awaiting_hr_confirm

HR 确认最后工作时间
  -> complete_task/hr_confirm
  -> handover_and_recovery

TL 完成交接
  -> complete_task/tl_done
  -> 如果 Ops 未完成，仍在 handover_and_recovery
  -> 如果 Ops 已完成，进入 awaiting_hr_signoff

Ops 完成回收
  -> complete_task/ops_done
  -> 如果 TL 未完成，仍在 handover_and_recovery
  -> 如果 TL 已完成，进入 awaiting_hr_signoff

HR 最终签核
  -> complete_task/hr_sign
  -> closed
```

超时扫描不改变主状态：

```text
scan_timeouts
  -> mark_tl_timeout / mark_ops_timeout
  -> reminder_count += 1
  -> 规定时间内第 3 次提醒起通知 HR 人工跟进
  -> overdue 后每次提醒都通知 HR 人工跟进
  -> 记录 escalated/escalations
```

所以 `case.status` 只表示生命周期：

| 字段 | 用途 |
| --- | --- |
| `case.phase` | 状态机主阶段 |
| `case.status` | `active` 或 `closed` |
| `case.escalated` | 是否发生过升级 |
| `case.escalations` | 具体升级记录 |

## Action 映射

| Action | Payload | 处理方式 |
| --- | --- | --- |
| `register_source` | 无 | 注册当前 source 到角色来源表 |
| `set_pending_intent` | 无 | 员工设置 case 创建前的临时离职意向 |
| `get_pending_intent` | 无 | 员工读取自己的临时离职意向；如果已过期会清理并返回 `pending_intent_expired` |
| `cancel_pending_intent` | 无 | 员工取消临时离职意向 |
| `init_case` | `employee_name`, `employee_id` | 员工创建 case；必须存在未过期 pending intent，否则返回 `pending_intent_required` 或 `pending_intent_expired` |
| `complete_task` | `task_type=hr_confirm`, `last_working_day`, `actor_name`, `actor_id` | HR 确认，记录 HR 负责人，进入 `handover_and_recovery` |
| `complete_task` | `task_type=tl_done`, `tl_summary`, `actor_name`, `actor_id` | TL 完成交接，记录 TL 负责人，必要时进入 `awaiting_hr_signoff` |
| `complete_task` | `task_type=ops_done`, `recovery_data`, `actor_name`, `actor_id` | Ops 完成回收，记录 Ops 负责人，必要时进入 `awaiting_hr_signoff` |
| `complete_task` | `task_type=hr_sign` | HR 结案签核，进入 `closed` |
| `scan_timeouts` | 无 | system 扫描超时并记录提醒/升级 |
| `get_status` | 无 | 返回当前角色可见的状态视图 |
| `get_audit_log` | 无 | 仅 admin 可读取完整 audit log |
| `get_archive_view` | `case_id` | 仅 admin 可按 case id 读取已关闭 case 的完整归档视图；active case 返回 `invalid_phase` |

## 模块职责

| 文件 | 职责 |
| --- | --- |
| `model.py` | 定义 case、枚举、步骤、通知、审计和返回结果模型 |
| `state.py` | 定义 `ResignationCaseMachine`，只描述合法状态流转 |
| `rules.py` | 角色解析、权限校验、payload 校验、关闭条件判断 |
| `effects.py` | 修改 case 字段、追加 audit、生成通知和归档 payload |
| `views.py` | 生成 employee/hr/tl/ops/admin 的角色视图 |
| `store.py` | 将 case 和 pending registry 存入 workspace 私有目录 |
| `orchestrator.py` | action 编排入口，连接 store/rules/state/effects/views |

## 角色权限

| 角色 | 可执行任务 |
| --- | --- |
| employee | `init_case`、pending intent 相关动作、读取状态 |
| hr | `hr_confirm`、`hr_sign`、读取状态 |
| tl | `tl_done`、读取状态 |
| ops | `ops_done`、读取状态 |
| admin | 只读状态和完整 audit |
| system | `scan_timeouts` |

Admin v1 不代替 HR/TL/Ops 执行业务任务。结案后 `scenario_engine` 会自动派发归档任务给 Admin Agent；Admin 负责调用 `get_archive_view` 在 Admin 窗口输出完整档案信息，并调用 `scenario_notify` 分别向 employee 和 HR 投递脱敏摘要。

## 业务身份记录

权限身份和业务展示身份是两套数据：

```text
source                 platform-telegram:hr/123
business identity      李四(ID: HR001)
```

`source` 仍是唯一可信的角色来源，决定谁能调用哪个 action。业务身份只用于通知、视图、负责人汇总和审计数据，不允许用来声明权限角色。

case 中的员工字段示例：

```json
{
  "employee": {
    "name": "张三",
    "id": "E001",
    "label": "张三(ID: E001)",
    "source": "platform-telegram:employee/123"
  }
}
```

HR/TL/Ops 完成动作后会写入 `responsible`：

```json
{
  "responsible": {
    "hr": {
      "name": "李四",
      "id": "HR001",
      "label": "李四(ID: HR001)",
      "source": "platform-telegram:hr/123"
    },
    "tl": {
      "name": "王五",
      "id": "TL001",
      "label": "王五(ID: TL001)",
      "source": "platform-telegram:tl/123"
    },
    "ops": {
      "name": "赵六",
      "id": "OPS001",
      "label": "赵六(ID: OPS001)",
      "source": "platform-telegram:ops/123"
    }
  }
}
```

Admin 的状态视图会额外返回 `responsible_summary`：

```json
{
  "responsible_summary": {
    "hr": "李四(ID: HR001)",
    "tl": "王五(ID: TL001)",
    "ops": "赵六(ID: OPS001)"
  }
}
```

## 归档流程

HR 最终签核后 case 进入 `closed`。如果已有 Admin source，`scenario_engine` 工具会自动向 Admin Agent 派发归档任务。Admin Agent 收到任务后按申请单号调用：

```json
{"action":"get_archive_view","payload":{"case_id":"RES-XXXXXXXX"}}
```

归档视图只允许 Admin 读取，并且只对已关闭 case 生效。返回内容包含完整 case、完整 audit log、负责人汇总和 TL/Ops 交接回收详情。Admin Agent 会先生成完整未脱敏报告，并调用 `scenario_notify` 投递到 Admin 窗口：

```json
{"target_role":"admin","case_id":"RES-XXXXXXXX","content":"完整未脱敏归档报告..."}
```

这份完整报告会同时持久化到：

```text
cases/archives/RES-XXXXXXXX/admin_full_report.md
```

Employee 和 HR 不读取完整归档视图。Admin 需要生成脱敏摘要后调用 `scenario_notify` 分别投递：

```json
{"target_role":"employee","case_id":"RES-XXXXXXXX","content":"脱敏归档摘要..."}
```

```json
{"target_role":"hr","case_id":"RES-XXXXXXXX","content":"脱敏归档摘要..."}
```

两份脱敏摘要会同时持久化到：

```text
cases/archives/RES-XXXXXXXX/employee_summary.md
cases/archives/RES-XXXXXXXX/hr_summary.md
```

脱敏摘要只保留 case_id、员工姓名/编号标签、最后工作时间、关键节点、HR/TL/Ops 负责人姓名+ID、交接/回收完成结论；不得包含 platform source、raw audit source、内部 registry 或完整原始 JSON。`scenario_notify` 只允许 Admin source 调用，只支持 `admin`、`employee` 和 `hr` 作为目标，不修改 case 状态，不写 audit。只有 `admin` 目标可以接收完整未脱敏报告。

## 返回结果

Scenario Orchestrator 的所有 action 返回 `ScenarioResult`：

```text
ok              是否成功
code            机器可读结果码
message         人类可读说明
case_id         当前 case id
view            查询类 action 的角色视图
notifications   需要发送给其他角色的通知意图
archive_payload HR sign 后生成的归档数据
errors          失败原因
```

Scenario Engine 生成 `notifications`，由 `scenario_engine` 工具转换成 EventBus `OutboundEvent` 进行投递。HR sign 产生 `archive_payload` 时，`scenario_engine` 还会额外发布一个面向 Admin Agent 的 `DispatchEvent` 来启动归档总结。

## Agent 工具

Agent 通过 `scenario_engine` 工具调用本引擎：

```json
{"action":"get_status","payload":{}}
```

```json
{"action":"set_pending_intent","payload":{}}
```

```json
{"action":"init_case","payload":{"employee_name":"张三","employee_id":"E001"}}
```

```json
{"action":"complete_task","payload":{"task_type":"hr_confirm","last_working_day":"2026/05/20:18:00","actor_name":"李四","actor_id":"HR001"}}
```

```json
{"action":"complete_task","payload":{"task_type":"tl_done","tl_summary":"交接摘要","actor_name":"王五","actor_id":"TL001"}}
```

```json
{"action":"complete_task","payload":{"task_type":"ops_done","recovery_data":{"account":"done","asset":"done"},"actor_name":"赵六","actor_id":"OPS001"}}
```

```json
{"action":"get_archive_view","payload":{"case_id":"RES-XXXXXXXX"}}
```

工具只接受 `action` 和 `payload`。真实角色身份来自当前会话的 `session.source`，不要在 payload 中传 `source`、`role` 或其他角色声明字段。`employee_name`、`employee_id`、`actor_name`、`actor_id` 是业务展示身份，不参与权限判断。

工具返回外包一层：

```json
{
  "scenario": {
    "ok": true,
    "code": "status",
    "message": "Status loaded."
  },
  "delivery": {
    "published": 0,
    "skipped": [],
    "errors": []
  }
}
```

`scenario` 是业务结果，`delivery` 是通知投递报告，记录成功投递数量和找不到目标窗口的 skipped 项。业务状态会先写入，再执行通知/归档派发；如果 EventBus 投递抛错，错误会直接向工具调用方抛出，不额外包装进 `delivery.errors`。
