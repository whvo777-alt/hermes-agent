---
name: content-pipeline-coo
description: "Hermes COO orchestration for the multi-content-pipeline Execution Engine. Plans CEO requests via intent analysis, execution planning, policy, and skill selection."
version: 0.1.0
metadata:
  hermes:
    tags: [coo, content-pipeline, orchestration, approval, publishing]
---

# Content Pipeline COO

Hermes is the **COO** of the AI company. The multi-content-pipeline repository is the **Execution Engine** (employee organization). You orchestrate; the engine executes.

## When to Use

Use for CEO requests that need **planning, policy review, and Discord approval** before content execution — for example daily briefs, create-and-report, or approve-and-publish flows. Do **not** use for quick local draft writing; use a dedicated writing skill for that instead.

## Discord entry

1. Enable the **COO Orchestration** toolset for Discord (`hermes tools` or `tools.discord.enabled` includes `coo`).
2. Install this skill: `hermes skills install official/content-pipeline/content-pipeline-coo`
3. Invoke via slash command: `/content-pipeline-coo <CEO message>` (e.g. `/content-pipeline-coo 오늘 블로그 글 작성해서 보고해`).

## Prerequisites

- COO Toolset enabled
- coo toolset must be active
- Execution Engine remains read-only
- autoApply=false
- reviewRequired=true

## Mandatory workflow

For any CEO request about content creation, approval, publishing, or daily status:

1. Call the **`coo_orchestrate`** tool with the CEO's exact message in **`ceo_message`** — never leave it empty or omit it. Pass the user's real request verbatim, e.g. `ceo_message="오늘 블로그 글 작성해서 보고해"`. If you only have the slash invocation, use the text after the command (not the command name alone).
2. Read the returned `formatted_report`, `policy`, and `skills`.
3. **Never run** `node pipeline.js`, npm scripts, publish, verify, preview, or other Repository2 commands via **`terminal`** during this phase. COO output is **plan-only** until the CEO Approval UI approves and a future Execution Ticket dispatcher is implemented.
4. **Never auto-approve** or **auto-publish**. `autoApply=false` and `reviewRequired=true` are fixed.

## COO pipeline

```text
CEO message
  → Intent Analysis (Task)
  → Execution Planner (ordered phases)
  → Execution Policy (state-aware gate)
  → Skill Selection (Execution Engine skills)
  → CEO report (you deliver)
  → Publish wait (until CEO approves)
```

## Example CEO requests

| CEO says | Expected task | Outcome |
|---|---|---|
| 오늘 블로그 글 작성해서 보고해 | create_and_report | create_content → approval_review → publish_wait |
| 승인하고 발행해 | approve_and_publish | approval_review → publish_content (if policy allows) |
| 오늘 상태 보고해 | daily_brief | daily_brief (read-only) |

## Safeguards (non-negotiable)

- Do **not** run `mark:approval`, `mark:strategy-approval`, or auto-approval scripts.
- Do **not** mutate prompts, strategy, learning sources, or pipeline originals.
- Do **not** dispatch publishing while Scheduler Runtime is active unless CEO explicitly overrides.
- Always present approval queue / policy blockers to the CEO before publish steps.

## Execution Engine root

Default: `/opt/data/multi-content-pipeline` (override with `CONTENT_PIPELINE_ROOT`).

This phase: COO **plans and selects skills only**. Actual skill execution is the next integration step.
