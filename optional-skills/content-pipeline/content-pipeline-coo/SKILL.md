---
name: content-pipeline-coo
description: "Hermes COO orchestration for CEO content requests (create/report/approve/publish). Plans intent, policy, and approval via the coo_orchestrate tool — content generation runs natively inside Hermes, never against Repository 2."
version: 0.2.0
metadata:
  hermes:
    tags: [coo, content-pipeline, orchestration, approval, publishing]
---

# Content Pipeline COO

Hermes generates and reports on blog content **natively**. There is no external "Execution Engine" in this flow — everything happens inside this repository: `tools/coo_tools.py` → `agent/content/orchestrator.py` (Research → Planning → Writing → Quality) → `agent/coo/daily_blog_bundle.py` (per-platform CEO approval).

## When to Use

Use for CEO requests that need **planning, policy review, and Discord approval** for content — for example daily briefs, create-and-report, or approve-and-publish flows. Do **not** use for quick local draft writing; use a dedicated writing skill for that instead.

## Discord entry

1. Enable the **COO Orchestration** toolset for Discord (`hermes tools` or `tools.discord.enabled` includes `coo`).
2. Install this skill: `hermes skills install official/content-pipeline/content-pipeline-coo`
3. Invoke via slash command: `/content-pipeline-coo <CEO message>` (e.g. `/content-pipeline-coo 오늘 블로그 글 작성해서 보고해`).

## Prerequisites

- COO Toolset enabled
- coo toolset must be active
- autoApply=false
- reviewRequired=true

## Mandatory workflow

For any CEO request about content creation, approval, publishing, or daily status:

1. Call the **`coo_orchestrate`** tool with the CEO's exact message in **`ceo_message`** — never leave it empty or omit it. Pass the user's real request verbatim, e.g. `ceo_message="오늘 블로그 글 작성해서 보고해"`. If you only have the slash invocation, use the text after the command (not the command name alone).
2. Read the returned `ceo_message`, `approval_report_markdown`, and — for content requests — `daily_blog_bundle.report_markdown`. That response is the complete result; there is nothing else to fetch, inspect, or run.
3. **Never** read, list, or otherwise inspect `/opt/data/multi-content-pipeline` (Repository 2) or any file under it — `package.json`, `run_report.md`, `discord_approval_queue.json`, `publishing_plan.md`, manifests, `outputs/`, or anything else. It does not back this flow at all; content is generated and stored entirely inside Hermes' own data directory by `agent/content/orchestrator.py`.
4. **Never run** `node pipeline.js`, npm scripts, publish, verify, preview, or other Repository2 commands via **`terminal`** during this phase — and if a command is ever blocked, do not reproduce its output structure by hand as a workaround. COO output is **plan-only**; approval and any future publish happen only through the sessions `coo_orchestrate` already returned.
5. **Never auto-approve** or **auto-publish**. `autoApply=false` and `reviewRequired=true` are fixed.

## COO pipeline

```text
CEO message
  → Intent Analysis (Task)
  → Execution Planner (ordered phases)
  → Execution Policy (state-aware gate)
  → CREATE_AND_REPORT: Research → Planning → Writing → Quality (agent/content/orchestrator.py)
  → CEO report + one approval session per platform (agent/coo/daily_blog_bundle.py)
  → Publish wait (until the CEO approves each item)
```

## Example CEO requests

| CEO says | Expected task | Outcome |
|---|---|---|
| 오늘 블로그 글 작성해서 보고해 | create_and_report | Research→Planning→Writing→Quality runs natively, then one approval item per platform |
| 승인하고 발행해 | approve_and_publish | approval_review → publish_content (if policy allows) |
| 오늘 상태 보고해 | daily_brief | daily_brief (read-only) |

## Safeguards (non-negotiable)

- Do **not** run `mark:approval`, `mark:strategy-approval`, or auto-approval scripts.
- Do **not** mutate prompts, strategy, learning sources, or pipeline originals.
- Do **not** read, write, or execute anything under `/opt/data/multi-content-pipeline` (Repository 2) for this flow — it is not part of the content-and-report path.
- Always present approval queue / policy blockers to the CEO before publish steps.
