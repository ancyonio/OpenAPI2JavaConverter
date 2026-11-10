---
mode: agent
agent: quality-reviewer
description: Review a spec or service against house API standards and static analysis.
---

Review `${input:target}` against house standards.

Run `o2j_quality_report`, then work through the checklist that static analysis cannot cover:
API contract, Spring idiom, security, persistence, observability and resilience.

Fix what is clearly wrong. Suppress a false positive narrowly, with a comment saying why.

Never lower a threshold, widen an exclusion or disable a rule to make a gate pass. If you think
a gate is wrong, make the case and stop.

Report findings by severity, what you fixed, what you suppressed and why, and what remains with
a recommendation for each.
