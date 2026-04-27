---
name: heartbeat-ops
description: Enable, update, and disable silent heartbeat background awareness
---

Help users manage Quclaw Heartbeat using existing file and shell tools. Do not
register or ask for a dedicated heartbeat tool.

## What is Heartbeat?

Heartbeat is one silent periodic background turn for the default agent. It is
for ongoing awareness: periodic self-checks, workspace maintenance, memory
organization, and batching several small checks into one agent turn.

Heartbeat is not a notification system. It does not send reminders or user-facing
messages. The runtime consumes heartbeat results internally.

## Heartbeat vs Cron

Use Heartbeat when the user wants:
- Ongoing periodic awareness
- Regular project or workspace health checks
- Background maintenance
- Several small checks batched into one turn
- A checklist that can evolve over time

Use Cron instead when the user wants:
- A specific task at a specific time
- A task with a cron expression
- Multiple independent scheduled jobs
- One-off work at an exact future time

If the user asks to be notified or reminded, explain that Heartbeat is silent in
the current runtime. Only create a heartbeat if silent background work still
satisfies the user's intent.

## Configuration

Heartbeat is controlled by `{{workspace}}/config.user.json`:

```json
{
  "heartbeat": {
    "interval_minutes": 30,
    "agent": null
  }
}
```

- `"interval_minutes": 0` disables Heartbeat.
- `"agent": null` uses `{{default_agent}}`.
- Use an explicit agent id only when the user asks for a specific agent.

The periodic checklist lives at `{{workspace}}/HEARTBEAT.md`.

## Enable or Update Heartbeat

1. Decide whether the request is Heartbeat or Cron using the rules above.
2. If it is Heartbeat, choose a conservative interval. Prefer 30 minutes unless
   the user gives a different interval.
3. Create or update `{{workspace}}/HEARTBEAT.md` with a concise checklist.
4. Update `{{workspace}}/config.user.json` so `heartbeat.interval_minutes` is
   greater than zero.

Use the `read` tool first when updating existing files. Preserve unrelated
configuration and checklist content.

## Disable Heartbeat

Set:

```json
{
  "heartbeat": {
    "interval_minutes": 0,
    "agent": null
  }
}
```

Do not delete `HEARTBEAT.md` unless the user explicitly asks to remove the
checklist.

## HEARTBEAT.md Template

```markdown
# HEARTBEAT

Silent background checklist for periodic awareness. Do not notify the user.

- Check whether any project notes or task files need small maintenance.
- Organize obvious stale scratch files only when safe.
- Batch small checks into one concise turn.
- If nothing needs attention, reply HEARTBEAT_OK.
```
