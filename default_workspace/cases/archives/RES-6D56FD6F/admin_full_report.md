# Admin Full Archive Report

- Case ID: RES-6D56FD6F
- Phase/Status: closed / closed
- Created At: 1778012360
- Escalated: yes
- Escalations: ops_timeout

## Employee
- Name: Alice
- ID: 9527
- Label: Alice(ID: 9527)
- Source: platform-telegram:employee/8275917140

## Final Working Day
- 2026/05/06:08:00:00

## Responsible Parties
- HR: hr1(ID: 0001)
- HR Source: platform-telegram:hr/8275917140
- TL: Bob(ID: 0002)
- TL Source: platform-telegram:tl/8275917140
- Ops: Rat(ID: 0005)
- Ops Source: platform-telegram:ops/8275917140

## TL Handover Summary
- Completed: yes
- Completed At: 1778012519
- Summary: TL 确认交接已完成，所有文档资料已交接完。
- Reminder Count: 1
- Last Reminded At: 1778012463
- Escalated: no

## Ops Recovery Details
- Completed: yes
- Completed At: 1778012594
- Recovery Data: {}
- Reminder Count: 3
- Last Reminded At: 1778012584
- Escalated: yes

## Step Status
- Step 1: done at 1778012360
- Step 2: done at 1778012453; HR confirmed: yes; deadline: 1778098760
- Step 3: done at 1778012594
- Step 4: done at 1778012650; HR signed: yes

## Escalation Record
- Escalated: yes
- Escalation Types: ops_timeout

## Full Audit Log
1. ts=1778012360 | actor_role=employee | source=platform-telegram:employee/8275917140 | event=init_case | data={"employee":{"name":"Alice","id":"9527","label":"Alice(ID: 9527)"}}
2. ts=1778012453 | actor_role=hr | source=platform-telegram:hr/8275917140 | event=hr_confirm | data={"last_working_day":"2026/05/06:08:00:00","deadline":1778025600,"actor":{"name":"hr1","id":"0001","label":"hr1(ID: 0001)"}}
3. ts=1778012463 | actor_role=system | source=cron:resignation-monitor | event=tl_reminder | data={"reminder_count":1,"overdue":false}
4. ts=1778012463 | actor_role=system | source=cron:resignation-monitor | event=ops_reminder | data={"reminder_count":1,"overdue":false}
5. ts=1778012519 | actor_role=tl | source=platform-telegram:tl/8275917140 | event=tl_done | data={"tl_summary":"TL 确认交接已完成，所有文档资料已交接完。","actor":{"name":"Bob","id":"0002","label":"Bob(ID: 0002)"}}
6. ts=1778012524 | actor_role=system | source=cron:resignation-monitor | event=ops_reminder | data={"reminder_count":2,"overdue":false}
7. ts=1778012584 | actor_role=system | source=cron:resignation-monitor | event=ops_reminder | data={"reminder_count":3,"overdue":false}
8. ts=1778012594 | actor_role=ops | source=platform-telegram:ops/8275917140 | event=ops_done | data={"recovery_data":{},"actor":{"name":"Rat","id":"0005","label":"Rat(ID: 0005)"}}
9. ts=1778012650 | actor_role=hr | source=platform-telegram:hr/8275917140 | event=hr_sign | data={}
