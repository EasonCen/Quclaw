# 试用期主动离职场景功能规格说明

**项目**：quclaw 场景化功能扩展  
**场景**：试用期员工主动离职  
**版本**：v4.0  
**状态**：待开发

---

## 一、背景与目标

### 背景

quclaw 当前是一个通用 Agent 对话框架，具备多 Agent 协作、事件驱动、Cron 定时任务、Telegram 渠道接入等能力。现有架构中缺少面向特定业务场景的结构化流程支持——所有逻辑都依赖 LLM 在对话中即兴判断，无法保证流程的确定性、角色隔离和异常处理。

### 目标

在 quclaw 现有架构基础上，增加一个**试用期主动离职场景**，作为场景化功能的第一个完整实现，验证以下能力：

- 多角色权限隔离：不同部门只能看到和操作属于自己职责范围的信息，隔离由代码层强制保证，不依赖 prompt 约束
- 人机分工明确：AI 负责自然语言的进出口，代码负责流程状态推进，人工负责关键确认与签核
- 多 Agent 协作：角色 Agent 之间存在真实的任务传递，结案阶段由 hr-agent dispatch archive-agent 完成汇总与归档
- 异常自动处理：超时自动催办，无需人工盯守

### 演示形式

使用一个 Telegram 账号，分别订阅 5 个 Bot，模拟加入员工、HR、TL、运维、后台管理五个窗口。每个 Bot 窗口只能看到和操作属于该窗口职责的内容。

---

## 二、核心概念

### Bot 即部门入口

每个 Bot 代表一个**角色部门的入口**，而非绑定到特定个人。用户订阅某个 Bot 即表示加入该部门，获得对应权限。`allowed_user_ids` 是部门成员名单，同一用户可以订阅多个 Bot 从而同时拥有多个部门的权限。

演示阶段每个窗口只有一名成员（同一个 Telegram 账号订阅全部 5 个 Bot），这在语义上等同于一个人身兼多职参与演示，不影响权限隔离的验证。

### 三层职责划分

```
┌──────────────────────────────────────────────────┐
│  人工层                                           │
│  正式确认 / 审批 / 结案签核 / 争议处理            │
├──────────────────────────────────────────────────┤
│  代码层（ScenarioEngine）                         │
│  状态机推进 / 条件判断 / 权限校验                 │
│  超时计算 / 通知路由 / 视角脱敏 / 审计日志        │
├──────────────────────────────────────────────────┤
│  AI 层（角色 Agent + Archive Agent）              │
│  意图识别 / 信息抽取 / 自然语言转述 / 报告生成    │
└──────────────────────────────────────────────────┘
```

**代码层**承担一切"输入确定则输出确定"的逻辑，不受 LLM 影响。

**AI 层**只做两件事：读懂人说的话，以及把结构化数据转成自然语言。AI 层不做任何流程决策。

**通知文案** v1 由代码层固定模板生成并直接投递，结案报告由 archive-agent 使用 LLM 生成，是场景中唯一使用 LLM 生成内容的通知环节。

### 多 Agent 协作结构

```
employee-agent ──→ ScenarioEngine ──→ hr-agent
                        ↑                 ↓
               tl-agent ┤    (结案时 subagent_dispatch)
               ops-agent┘                ↓
                                   archive-agent
                                    ↙         ↘
                           管理结案报告    员工时间线摘要
```

角色 Agent 各自独立处理所在窗口的对话，但不直接读写 case 文件。`cases/` 是 ScenarioEngine 的私有持久化存储，所有角色只能通过 ScenarioEngineTool 获取本角色允许看到的视图或执行允许的动作。结案节点是唯一存在 Agent 间直接任务传递的环节：hr-agent 在完成签核后 dispatch archive-agent，并将 ScenarioEngine 返回的归档 payload 传给 archive-agent，由 archive-agent 生成报告并分别投递给 admin_bot 和 employee_bot。

---

## 三、系统组件

| 组件 | 类型 | 职责 |
|------|------|------|
| TelegramMultiBotChannel | 渠道层 | 同时监听 5 个 Bot，处理 `/start` 窗口注册，携带 bot_key 路由 |
| ScenarioEngine | 业务模块 | 状态机核心，所有流程逻辑与 case 原始数据访问的唯一执行点，纯 Python，无 LLM 依赖 |
| ScenarioEngineTool | 工具适配层 | 将 ScenarioEngine 封装为 quclaw 标准工具供 Agent 调用，传入真实 EventSource，不暴露 case 文件路径 |
| employee-agent | Agent | 员工部门窗口：意图识别、确认 hook、状态转述 |
| hr-agent | Agent | HR 执行窗口：申请确认、最后工作日确认、结案签核、dispatch archive-agent |
| tl-agent | Agent | TL 部门窗口：引导填写交接摘要、标记完成 |
| ops-agent | Agent | 运维部门窗口：引导确认回收清单、标记完成 |
| admin-agent | Agent | 后台 / 管理窗口：查询完整状态、审计日志、异常升级记录 |
| archive-agent | Agent | 结案归档：基于 ScenarioEngine 返回的归档 payload 生成管理报告与员工时间线，分别投递 |
| resignation-monitor | Cron | 每 30 分钟触发超时扫描 |
| `cases/` 目录 | 私有持久化存储 | Case 原始状态文件，仅 ScenarioEngine/CaseStore 可读写，其他组件不得直接访问 |

---

## 四、部门与 Bot 设计

### 五个窗口的职责边界

| Bot | 部门定位 | 对话模式 | 可见信息 | 可执行操作 |
|-----|---------|---------|---------|-----------|
| employee_bot | 员工窗口 | 主动表达意向；被动接收通知 | 自己的步骤状态、待办事项、最终时间线摘要 | 表达离职意向、确认或取消进入流程、查询本人进度 |
| hr_bot | HR 执行窗口 | 被动接收审批/签核任务；主动回复确认 | 与 HR 执行任务相关的信息、交接摘要、回收完成状态、需要 HR 跟进的异常 | 确认申请与最后工作日、结案签核、人工跟进异常 |
| tl_bot | 交接执行窗口 | 被动接收交接任务；主动标记完成 | 交接任务详情、员工基本工作信息 | 填写交接摘要、标记交接完成 |
| ops_bot | 权限回收执行窗口 | 被动接收回收任务；主动标记完成 | 权限与资产回收清单及完成状态 | 逐项确认回收、标记回收完成 |
| admin_bot | 后台 / 管理窗口 | 主动查询；被动接收管理报告和升级抄送 | 完整 case 信息、所有角色操作、审计日志、异常升级记录、管理结案报告 | 查询状态、查看日志、查看异常升级；v1 不直接代替执行角色完成任务 |

### 后台 / 管理窗口

admin_bot 是独立的后台管理视角，不等同于 HR。HR 负责正式确认和结案签核，admin 负责完整查询和审计查看：

- **状态查询**：Admin 主动发送 `/status`，admin-agent 调用 `get_status` 返回完整管理视图
- **日志查询**：Admin 主动发送 `/log`，admin-agent 调用 `get_audit_log` 或 `get_status` 的管理视图查看审计日志
- **升级接收**：TL/Ops 超时升级时，admin_bot 收到抄送，HR 仍收到需要人工跟进的执行通知

---

## 五、Telegram 多 Bot 渠道

### 配置格式

```json
{
  "channels": {
    "telegram": {
      "enabled": true,
      "bots": {
        "employee": { "bot_token": "...", "allowed_user_ids": ["..."] },
        "hr":       { "bot_token": "...", "allowed_user_ids": ["..."] },
        "tl":       { "bot_token": "...", "allowed_user_ids": ["..."] },
        "ops":      { "bot_token": "...", "allowed_user_ids": ["..."] },
        "admin":    { "bot_token": "...", "allowed_user_ids": ["..."] }
      }
    }
  }
}
```

旧的单 `bot_token` 格式保持向后兼容，解析为无 bot_key 的默认 Bot，现有行为不变。

### Event Source 格式

新格式：
```
platform-telegram:<bot_key>/<chat_id>
platform-telegram:<bot_key>/<chat_id>/<thread_id>
```

旧格式继续可解析：
```
platform-telegram:<chat_id>
platform-telegram:<chat_id>/<thread_id>
```

### `/start` 处理

`/start` 在 channel 层显式注册 command handler 处理，不依赖 Agent 的 prompt 指令保证执行。

收到 `/start` 后：

1. 从 event source 读取 `bot_key`，确定用户加入的部门
2. 调用 `ScenarioEngine.register_source(role, source)` 完成部门成员注册
3. 返回该部门窗口的欢迎语和当前可操作事项（固定模板）
4. 不触发 case 创建

### 定向投递

OutboundEvent 投递时，根据目标 source 字符串中的 `bot_key` 反查对应 Bot 实例发出消息。ScenarioEngine 只需要知道目标 source 字符串，投递层自动完成路由。

### Routing 配置

```
platform-telegram:employee/.*  →  employee
platform-telegram:hr/.*        →  hr
platform-telegram:tl/.*        →  tl
platform-telegram:ops/.*       →  ops
platform-telegram:admin/.*     →  admin
```

---

## 六、流程步骤详细说明

### 步骤 1：接收离职意向（含确认 Hook）

**触发**：员工在 employee_bot 中以任意自然语言表达离职相关意图。

**AI 层（意图识别）**：employee-agent 判断消息是否包含离职意向。判断结果为二值：识别到意向 / 未识别到。未识别到则正常对话，不进入流程。

**确认 Hook**：识别到意向后，employee-agent **不立即创建 case**，而是先向员工发送确认消息：

```
检测到您可能希望提交离职申请。

请回复：
1 - 确认，进入离职申请流程
2 - 取消，我只是随便说说
```

**关键词检测**：employee-agent 收到下一条消息后，进行硬关键词匹配：

- 消息内容为 `1`：调用 `ScenarioEngine.init_case()`，流程正式开始
- 消息内容为 `2`：回复「好的，如有需要随时告知」，不创建 case，流程结束
- 其他内容：回复「请回复 1 确认或 2 取消」，等待重新输入

关键词检测由 employee-agent 在 AGENT.md 中以硬规则描述，不依赖 LLM 语义判断，`1` 就是 `1`，`2` 就是 `2`。

**确认 Hook 的状态管理**：employee-agent 在等待员工回复 1 或 2 期间，处于"待确认"中间态。此状态记录在 case 文件之外（因为 case 尚未创建），由 ScenarioEngine 管理并持久化到私有 `pending_sources.json` 的 `pending_intent` 字段。employee-agent 每次收到消息时通过 ScenarioEngineTool 查询当前 source 是否处于待确认阶段，不直接读取该文件。

**代码层（init_case）**：员工确认后，ScenarioEngine 创建 case 文件，设定步骤 2 deadline（24 小时），清除 `pending_intent`，向 HR 发送固定模板通知。

**AI 层（转述）**：employee-agent 收到 ScenarioEngine 返回的结构化结果后，向员工发送：「申请已受理，HR 将在 24 小时内确认，请留意通知。」

**结束状态**：case status 为 `active`，step_1 为 `done`，step_2 为 `active`，HR 收到新案例通知。

---

### 步骤 2：确认申请与最后工作日

**触发**：HR 在 hr_bot 收到通知后，以自然语言回复确认并提供最后工作日。

**AI 层**：hr-agent 从 HR 的回复中抽取最后工作日日期和确认意图。抽取完成后，在调用工具前向 HR 明确提示：「请确认：最后工作日为 XX 月 XX 日，此操作正式确认后不可撤回。」等待 HR 二次确认后调用 `complete_task(hr_confirm)`。

**代码层**：记录最后工作日，`hr_confirmed = true`，检查步骤完成条件满足后推进至步骤 3，设定步骤 3 deadline（72 小时），**同时**向 tl_bot 和 ops_bot 发送任务通知（并行解锁）。

**结束状态**：step_2 为 `done`，step_3 为 `active`，TL 和 Ops 各自收到任务通知。

---

### 步骤 3：交接与权限回收（并行）

TL 和 Ops 的任务同时进行，互不依赖，各自独立计算超时。

**TL 侧**：tl-agent 收到通知后引导 TL 填写接管人员、工作内容、知识库地址等结构化信息（自由文本，不做格式校验）。TL 确认填写完整后，tl-agent 调用 `complete_task(tl_done)`，将摘要文本作为 `data` 传入。

**Ops 侧**：ops-agent 收到通知后展示权限回收清单（GitHub、内网、邮箱等）。Ops 逐项确认完成后，ops-agent 调用 `complete_task(ops_done)`。

**代码层**：`tl_done` 和 `ops_done` 独立维护。任意一方完成不触发步骤推进。**两者均为 true** 时，向 hr_bot 发送结案通知，推进至步骤 4。

**超时处理**：TL 和 Ops 各自独立超时计数，详见第九节。

**结束状态**：step_3 为 `done`，step_4 为 `active`，HR 收到结案通知。

---

### 步骤 4：结案与归档（含多 Agent 协作）

**触发**：HR 在 hr_bot 收到结案通知后，查看交接摘要，发出签核指令。

**AI 层（hr-agent）**：向 HR 展示完整交接摘要和 Ops 回收清单，等待 HR 明确签核。签核前提示「此操作为最终结案，不可撤回」。收到签核后调用 `complete_task(hr_sign)`。

**代码层**：验证全部关闭条件（见第八节）。全部满足后，case status 变更为 `closed`，写入最终 audit_log。返回结构化归档数据给 hr-agent。

**多 Agent 协作（hr-agent dispatch archive-agent）**：

hr-agent 收到 ScenarioEngine 返回"case 已关闭"的结构化结果后，通过 `subagent_dispatch` 调用 archive-agent，并将 ScenarioEngine 返回的归档 payload 作为上下文传入。该 payload 是由代码层组装的归档视图，不等同于 `cases/` 原始文件。

archive-agent 收到任务后，依次执行：

1. **生成管理结案报告**：使用 LLM 将归档 payload 整理为正式报告，包含完整时间线、交接人信息、权限回收确认列表、各步骤耗时统计
2. **生成员工时间线摘要**：使用 LLM 生成脱敏版本，只包含对员工有意义的关键节点
3. **投递管理报告**：通过 admin_bot 发送给后台管理窗口
4. **投递员工摘要**：通过 employee_bot 发送给员工

archive-agent 完成后返回归档结果给 hr-agent，hr-agent 在 hr_bot 侧确认「归档完成」。

**员工收到的时间线摘要格式示例**：

```
✅ 03-15  提交离职申请
✅ 03-15  HR 确认，最后工作日：03-22
✅ 03-18  工作交接完成
✅ 03-19  系统权限回收完成
✅ 03-20  流程正式结案

离职证明将在 3 个工作日内发放，如有疑问请联系 HR。
```

**Admin 收到的管理结案报告内容**：完整时间线、员工信息、最后工作日、交接人与交接摘要、权限回收清单确认、各步骤实际耗时、是否有过超时或升级事件。

**结束状态**：case status 为 `closed`，员工收到时间线摘要，admin 收到完整管理结案报告，HR 收到归档完成确认，归档完成。

---

## 七、多 Agent 协作说明

### 协作链路

```
employee-agent ──→ ScenarioEngine ──→ hr-agent
                        ↑                 ↓
               tl-agent ┤    (结案时 subagent_dispatch)
               ops-agent┘                ↓
                                   archive-agent
                                    ↙         ↘
                           管理结案报告    员工时间线摘要
```

步骤 1：员工自然语言 → employee-agent 识别意图 → ScenarioEngine.init_case()
步骤 2：HR 自然语言 → hr-agent 抽取数据 → ScenarioEngine.complete_task(hr_confirm)
步骤 3：tl-agent / ops-agent 并行写入 ScenarioEngine，两者均完成后 ScenarioEngine 通知 hr-agent
步骤 4：hr-agent 签核 → ScenarioEngine.complete_task(hr_sign) → subagent_dispatch archive-agent
        → 生成管理报告 + 员工摘要 → 分别投递至 admin_bot 和 employee_bot

### 为什么这样设计

步骤 1-3 中各角色 Agent 不需要感知彼此的存在，各自处理所在部门的对话即可，状态共享完全通过 ScenarioEngine 完成。这样设计使每个 Agent 的职责单一，AGENT.md 可以写得非常干净。

结案阶段引入 agent 间直接 dispatch，原因是归档任务需要：消费 ScenarioEngine 生成的归档 payload、使用 LLM 生成两份不同视角的文档、向两个不同 Bot 投递结果。这三件事组合在一起，交给一个专门的 archive-agent 处理，比在 hr-agent 里堆砌逻辑更清晰，也体现了"Agent 向另一个 Agent 分发专项任务"的协作模式。

---

## 八、关闭条件

Case 只有在以下 4 个条件**全部**满足时，才允许进入 `closed` 状态：

1. HR 已完成步骤 2 的正式确认（`hr_confirmed = true`）
2. TL 已完成交接信息填写（`tl_done = true`）
3. Ops 已完成权限与资产回收（`ops_done = true`）
4. HR 已完成步骤 4 的最终签核（`hr_signed = true`）

ScenarioEngine 在每次 `complete_task` 调用后自动检查，缺少任意一项则拒绝关闭并返回明确错误信息。

---

## 九、异常处理

v1 实现以下两种超时异常，由 resignation-monitor cron 每 30 分钟触发 `scan_timeouts` 检测：

**TL 超时未填写交接信息**（步骤 3，deadline 72 小时）

- 第 1～3 次超时：向 tl_bot 发催办通知，`tl_reminder_count += 1`，写入 audit_log
- 第 3 次后：步骤状态标记为 `escalated`，向 hr_bot 发人工跟进通知，并向 admin_bot 抄送升级记录，写入 audit_log

**Ops 超时未完成权限回收**（步骤 3，deadline 72 小时）

- 第 1～3 次超时：向 ops_bot 发催办通知，`ops_reminder_count += 1`，写入 audit_log
- 第 3 次后：步骤状态标记为 `escalated`，向 hr_bot 发人工跟进通知，并向 admin_bot 抄送升级记录，写入 audit_log

两种超时独立处理，互不影响。

---

## 十、ScenarioEngine

### 定位

ScenarioEngine 是整个场景的状态机核心，是所有流程逻辑的唯一执行点，也是 `cases/` 原始数据的唯一访问入口。所有角色 Agent 都通过 ScenarioEngineTool 调用它，不得绕过直接读写 case 文件。

业务逻辑封装在 `src/scenario/engine.py`，与工具适配层 `src/tools/scenario_engine_tool.py` 分离，便于独立单元测试。

`src/scenario/store.py` 封装 case 文件读写、文件级锁和原子写入。除 ScenarioEngine/CaseStore 外，其他模块不得 import 或直接操作 `cases/`。

### 支持的 Action

| action | 调用时机 | 调用方 |
|--------|---------|-------|
| `register_source` | 用户向某 Bot 发送 `/start` | TelegramMultiBotChannel |
| `init_case` | 员工回复「1」确认进入流程 | employee-agent |
| `complete_task` | 任意角色完成一个任务节点 | hr / tl / ops agent |
| `get_status` | 任意角色查询当前进度或管理视图 | 任意角色 agent |
| `get_audit_log` | 查询完整审计日志 | admin-agent |
| `scan_timeouts` | 定时超时扫描 | resignation-monitor cron |

### 权限校验

每次调用时，ScenarioEngine 从 `session.source` 读取真实 `bot_key` 作为角色来源，不信任 `payload.actor_role` 或 `viewer_role`。校验失败直接返回错误，不执行任何状态变更。

### 视角脱敏

`get_status` 在代码层按 `viewer_role` 返回脱敏视图：

| 角色 | 可见内容 |
|------|---------|
| employee | 当前步骤、自己的待办、最终时间线摘要。不含 HR 审批内容、审计日志、内部异常 |
| tl | 交接任务详情、员工基本工作信息 |
| ops | 权限与资产回收清单及完成状态 |
| hr | HR 执行任务所需信息：申请确认、最后工作日、交接摘要、回收完成状态、需要人工跟进的异常。不含完整审计日志 |
| admin | 完整 case 信息、所有角色操作、审计日志、异常升级记录、管理结案报告 |

### 并发安全

步骤 3 中 TL 和 Ops 可能同时回复，使用 `asyncio.Lock` 或 `filelock` 保证读-改-写的原子性。

---

## 十一、Case 文件

### 访问语义

`cases/` 是 ScenarioEngine 的私有持久化目录，不是各组件共享读写的公共配置层。它承担"唯一事实来源"的角色，但这个事实来源只能由 ScenarioEngine 读取和修改。

- 角色 Agent 不直接读写 `cases/`，只能调用 ScenarioEngineTool。
- ScenarioEngineTool 不暴露 case 文件路径，不返回原始 case JSON。
- `get_status` 返回按角色脱敏后的 view model。
- `complete_task(hr_sign)` 成功关闭流程后，ScenarioEngine 返回专供归档使用的 archive payload。
- archive-agent 只消费 archive payload，不直接读取 `cases/`。
- Cron 只调用 `scan_timeouts`，不直接扫描或修改 case 文件。
- v1 暂不改动 quclaw 的全局工具注册机制；本场景通过 AGENT.md 明确要求角色 Agent 只能使用 ScenarioEngineTool 访问流程数据。通用文件工具和 shell 工具隔离作为后续框架能力增强。


### 存放路径

```
quclaw_workspace/cases/RES-{id}.json
```

### 数据结构

```
case_id             string
status              active | closed | escalated
created_at          ISO 8601
employee            { name, id }

role_sources        { employee: [...], hr: [...], tl: [...], ops: [...], admin: [...] }

steps
  step_1
    status          done
    completed_at    ISO 8601

  step_2
    status          active | done
    deadline        ISO 8601（创建后 24 小时）
    hr_confirmed    boolean
    last_working_day string
    reminder_count  integer

  step_3
    status          waiting | active | done | escalated
    deadline        ISO 8601（步骤 2 完成后 72 小时）
    tl_done         boolean
    tl_summary      string
    tl_reminder_count integer
    ops_done        boolean
    ops_reminder_count integer

  step_4
    status          waiting | active | done
    hr_signed       boolean

audit_log           array，仅追加
  [ { ts, actor, event, data } ]
```

### 待确认中间态

员工触发意图识别但尚未回复 1 或 2 时，case 文件尚未创建，中间态由 ScenarioEngine 暂存于私有文件：

```
quclaw_workspace/pending_sources.json
  pending_intent: { source, detected_at }
```

employee-agent 每次收到消息时先调用 ScenarioEngineTool 查询该 source 是否处于待确认阶段。员工回复 1 后，`init_case` 创建 case 同时清除 `pending_intent`。员工回复 2 后，通过 ScenarioEngine 清除 `pending_intent`，不创建 case。

---

## 十二、各角色 Agent 设计

**employee-agent**
- 识别员工自然语言中的离职意向（二值判断，识别到 / 未识别到）
- 识别到后进入确认 Hook：发送固定格式确认消息，等待员工回复
- 硬关键词检测：`1` → `init_case`，`2` → 取消，其他 → 提示重新输入
- 员工查询进度时调用 `get_status(viewer_role=employee)` 后转述

**hr-agent**
- 被动响应模式：识别 HR 的确认或签核意图，抽取结构化数据，二次提示不可撤回后调用 `complete_task`
- 查询模式：调用 `get_status` 只返回 HR 执行视图，不返回完整审计日志
- 结案后 dispatch archive-agent，将 ScenarioEngine 返回的 archive payload 作为上下文传入，等待归档完成

**tl-agent**
- 收到任务通知后引导 TL 填写交接信息
- 确认填写完整后调用 `complete_task(tl_done)`，摘要文本作为 data 传入

**ops-agent**
- 收到任务通知后展示权限回收清单
- Ops 确认各项完成后调用 `complete_task(ops_done)`

**admin-agent**
- 提供后台 / 管理窗口
- 收到 `/status` 时调用 `get_status`，返回完整管理视图
- 收到 `/log` 时调用 `get_audit_log` 或管理视图，展示审计日志
- 接收超时升级抄送和管理结案报告
- v1 不代替 HR、TL、Ops 完成任务节点

**archive-agent**
- 接受 hr-agent 的 dispatch 调用，接收 ScenarioEngine 生成的 archive payload
- 使用 LLM 生成后台管理版结案报告（完整视图）
- 使用 LLM 生成员工版时间线摘要（脱敏视图）
- 通过 admin_bot 投递管理报告，通过 employee_bot 投递摘要
- 返回归档完成结果给 hr-agent

---

## 十三、Cron 配置

**名称**：resignation-monitor  
**执行周期**：每 30 分钟  
**关联 Agent**：admin-agent  
**行为**：调用 `scan_timeouts`，引擎完成超时判断、催办通知、升级处理，扫描摘要记录至 audit_log

Cron 本身不做任何业务判断，只负责定时触发。

---

## 十四、演示 SOP

正式演示前需按以下顺序完成初始化，确保所有 source 注册完毕：

1. 向 employee_bot 发送 `/start`
2. 向 hr_bot 发送 `/start`
3. 向 tl_bot 发送 `/start`
4. 向 ops_bot 发送 `/start`
5. 向 admin_bot 发送 `/start`

5 个 Bot 均完成注册后，再从 employee_bot 开始正式流程。

---

## 十五、范围限制

- **多案例并发**：同一时间只处理一个 active case
- **真实身份验证**：Bot 成员资格即权限来源，不验证 Telegram 用户与组织角色的绑定关系
- **结构化表单校验**：TL 和 Ops 的填写内容为自由文本，不做字段格式校验
- **飞书渠道**：本次只做 Telegram
- **多场景支持**：ScenarioEngine 只实现"试用期主动离职"这一个固定场景
- **通知文案定制**：步骤推进通知使用固定模板；仅管理结案报告和员工摘要使用 LLM 生成

---

## 十六、风险与缓解

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| 多 Bot polling 在同一 event loop 中互相阻塞 | 中 | 高 | 每个 Bot Application 独立 asyncio task；Phase 1 完成后压测 5 Bot 同时收消息 |
| Case 文件并发写导致状态丢失 | 中 | 高 | 读写集中在 ScenarioEngine/CaseStore；文件级锁 + 写临时文件后原子重命名，两者同时使用 |
| 员工连续发两条消息跳过确认 Hook | 低 | 中 | pending_intent 带 `detected_at` 时间戳，5 分钟内有效，过期自动清除 |
| archive-agent dispatch 超时（LLM 生成耗时） | 低 | 低 | subagent_dispatch 设置超时，超时后 hr-agent 提示"归档生成中，稍后发送"，不阻断结案 |
| 角色尚未 `/start` 导致通知无法投递 | 低 | 中 | ScenarioEngine 跳过并记录 warning，不阻断流程；演示 SOP 要求先完成全部注册 |
| LLM 绕过 ScenarioEngine 直接操作 case 文件 | 中 | 中 | v1 不改全局工具注册，AGENT.md 明确禁止直接访问 `cases/`；ScenarioEngineTool 不暴露文件路径；后续增加按 Agent 的工具白名单 |


---

## 十七、实施顺序

```
Phase 1  TelegramMultiBotChannel （Done）
         可验证：5 个 Bot 同时收发消息，/start 完成窗口注册，
                 event source 携带 bot_key，routing 正确分流

Phase 2  ScenarioEngine 核心模块
         可验证：pytest 直接调用模块，状态机按预期推进，
                 私有 case 文件正确更新，权限校验正确拒绝非法操作

Phase 3  ScenarioEngineTool + EventBus 投递
         可验证：Agent 调用工具后，目标窗口的 Bot 收到通知消息

Phase 4  employee-agent（含确认 Hook）+ routing
         可验证：意图识别 → 推送确认消息 → 回复 1 创建 case → 回复 2 取消

Phase 5  hr-agent + tl-agent + ops-agent + admin-agent
         可验证：端到端跑通步骤 1 → 步骤 2 → 步骤 3 → 步骤 4 签核

Phase 6  archive-agent（多 Agent 协作）
         可验证：hr-agent dispatch archive-agent，admin 收到管理结案报告，
                 员工收到时间线摘要

Phase 7  resignation-monitor Cron + 超时催办
         可验证：人为将 deadline 设为过去时间，催办通知正确发出，
                 3 次后 HR 收到人工跟进通知，admin 收到升级抄送

Phase 8  端到端录屏演示 + 边界 case 修复
         验证点：确认 Hook、信息隔离、多 Agent 协作、关闭条件、超时升级
```

---

## 十八、测试计划

| 测试项 | 验证内容 |
|--------|---------|
| Source 格式解析 | 新旧两种 source 格式均可正确解析和序列化 |
| 多 Bot 投递路由 | OutboundEvent 按 bot_key 选择正确的 Bot 实例发送 |
| `/start` 注册 | 完成部门成员注册，source 通过 ScenarioEngine 正确写入私有注册状态 |
| 确认 Hook 流程 | 识别到意向 → 发确认消息 → 回复 1 创建 case；回复 2 取消；其他提示重输 |
| pending_intent 超时清除 | 超过 5 分钟未回复，pending_intent 自动清除，下次表达意向重新触发 |
| init_case 权限 | 只能由 employee 部门发起，其他部门调用返回错误 |
| 步骤依赖校验 | HR 未确认时，TL/Ops 调用 `complete_task` 被拒绝 |
| 并行任务独立 | TL 完成但 Ops 未完成时，步骤 3 不推进至步骤 4 |
| 关闭条件完整性 | 4 个条件缺任意一个时，不允许 case 变为 closed |
| 视角脱敏 | `get_status(viewer_role=employee)` 不包含 HR 审批内容与审计日志 |
| 后台管理视图 | admin-agent 可以查看完整状态、审计日志和异常升级记录 |
| HR 执行视图 | hr-agent 可以查看确认/签核所需信息，但不返回完整审计日志 |
| 超时催办 | `scan_timeouts` 对超时步骤发出催办通知，reminder_count 正确递增 |
| 超时升级 | reminder_count 达到 3 后变为 escalated，HR 收到人工跟进通知，admin 收到升级抄送 |
| archive-agent dispatch | hr-agent 结案后将 archive payload dispatch 给 archive-agent，管理报告和员工摘要正确生成并投递 |
| 完整流程 | 员工意向 → 确认 Hook → HR 确认 → TL/Ops 并行 → HR 结案 → 归档 → 员工收到摘要 |

---

## 十九、Assumptions

- v1 只支持一个 active case，不支持多人同时发起离职流程
- 步骤推进通知使用固定模板；管理结案报告和员工摘要使用 LLM 生成，是场景中唯一使用 LLM 生成通知内容的环节
- v1 不做真实组织身份验证，订阅 Bot 即拥有对应部门权限
- 演示阶段每个窗口只有一名成员（同一 Telegram 账号）
- 确认 Hook 使用硬关键词检测（`1` / `2`），不使用 LLM 语义判断
- ScenarioEngine 业务模块（`engine.py`）与工具适配层（`scenario_engine_tool.py`）分离，便于独立测试
- 演示 SOP 要求 5 个 Bot 全部完成 `/start` 注册后再开始正式流程
