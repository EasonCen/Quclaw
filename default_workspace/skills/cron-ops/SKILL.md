---
name: cron-ops
description: Create, list, and delete scheduled cron jobs
---

Help users manage scheduled cron jobs in Quclaw using the existing file and shell
tools. Do not register or ask for a dedicated cron tool.

## What is a Cron?

A cron is a scheduled task that runs at specified intervals. Crons are stored as
`CRON.md` files at `{{crons_path}}/<name>/CRON.md`.

## Cron vs Heartbeat

Use Cron when the user wants a specific task at a specific time, a task with a
cron expression, multiple independent scheduled jobs, or one-off work at an
exact future time.

Use the `heartbeat-ops` skill instead when the user wants ongoing periodic
awareness, background maintenance, regular project checks, or several small
checks batched into one agent turn.

## Schedule Syntax

Standard cron format: `minute hour day month weekday`

Examples:
- `0 9 * * *` - Every day at 9:00 AM
- `*/30 * * * *` - Every 30 minutes
- `0 0 * * 0` - Every Sunday at midnight

Schedules must use exactly 5 fields. Keep the minimum granularity at 5 minutes
or slower; `* * * * *` and `*/1 * * * *` are invalid for this project.

## One-Off Jobs

Set `one_off: true` for jobs that should run only once. After dispatch, the cron is automatically deleted.

Use this for:
- Scheduled one-time tasks
- Delayed maintenance or data collection
- Background checkpoints at a specific future time

## Operations

### Create

1. Ask what task should run and when
2. Determine the schedule
3. Ask which agent should run the task
4. Ask for a brief description of what the cron does
5. If the task should run only once at a specific time, set `one_off: true`
6. Pick a filesystem-safe cron name using lowercase letters, numbers, `-`, and `_`
7. Use the `write` tool to create `{{crons_path}}/<cron-name>/CRON.md`

When creating the file, pass the full path to `write`, set `create_dirs: true`,
and write the complete CRON.md content from the template below.

### List

Use the `bash` tool to inspect the cron directory. Prefer this command shape:
```bash
python -c "from pathlib import Path; base=Path(r'{{crons_path}}'); print('\n'.join(sorted(p.name for p in base.iterdir() if (p / 'CRON.md').is_file())) or 'No cron jobs configured.')"
```

To show details for one cron, use the `read` tool on:
`{{crons_path}}/<cron-name>/CRON.md`

### Delete

1. List available crons
2. Confirm which one to delete
3. Use the `bash` tool with a safety check that only deletes inside `{{crons_path}}`

Use this command shape, replacing `<cron-name>` with the confirmed cron name:
```bash
python -c "from pathlib import Path; import shutil; base=Path(r'{{crons_path}}').resolve(); target=(base / '<cron-name>').resolve(); assert target != base and base in target.parents, target; shutil.rmtree(target)"
```

## Cron Prompt Guidelines

Cron jobs run silently in the background with no conversation context. The agent's final response is consumed internally as a `DispatchResultEvent` for runtime bookkeeping and is not delivered to the user.

**When the user asks to be notified** (e.g., "tell me", "let me know", "remind me"):
- Explain that cron jobs do not send user-facing notifications in the current runtime.
- Do not create a cron that promises direct delivery to the user.
- Offer to create a silent background task only if that still satisfies the user's intent.

**For normal cron jobs:**
- Write the prompt for autonomous background work.
- Prefer prompts that update files, inspect state, organize data, or perform maintenance.
- Make the final response a brief status summary for internal logs.

## Cron Template

```markdown
---
name: Cron Name
description: Brief description of what this cron does
agent: {{default_agent}}
schedule: "0 9 * * *"
one_off: false  # Set to true for one-time jobs (optional, defaults to false)
---

Task description for the agent to execute.
```

**Background daily summary:**
```markdown
---
name: Daily Summary
description: Writes a daily summary of activity
agent: {{default_agent}}
schedule: "0 9 * * *"
---

Review recent workspace activity and write a concise summary to `{{workspace}}/daily-summary.md`.
```

**One-off checkpoint:**
```markdown
---
name: Project Checkpoint
description: Runs one scheduled project checkpoint
agent: {{default_agent}}
schedule: "30 14 21 3 *"
one_off: true
---

Inspect project status files and append a concise checkpoint to `{{workspace}}/cron-checkpoints.md`.
```
