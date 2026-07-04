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

## Prerequisites

- COO Toolset enabled
- coo toolset must be active
- Execution Engine remains read-only
- autoApply=false
- reviewRequired=true

## Mandatory workflow

For any CEO request about content creation, approval, publishing, or daily status:

1. Call the **`coo_orchestrate`** tool with the CEO's exact message.
2. Read the returned `formatted_report`, `policy`, and `skills`.
3. **Do not bypass COO** by running pipeline commands directly unless COO selected the skill and policy allows it.
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
