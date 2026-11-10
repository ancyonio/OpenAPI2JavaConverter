---
mode: agent
agent: test-author
description: Raise coverage and mutation score on a class to the configured thresholds.
---

Bring `${input:className}` up to the thresholds in `o2j.yaml`: 85% line, 80% branch, 90% line
for the service layer, 70% mutation score.

Start from `o2j_coverage_report` and the operation's `requiredTests` list. Add cases for
behaviour that is not yet asserted, not for lines that are not yet executed — those are
different problems and only the first one is worth solving.

If line coverage is already high but the mutation score is low, the existing assertions are too
weak. Strengthen them instead of adding tests.

Do not modify production code. If a class cannot be tested as written, name the specific change
needed and why, then stop.

Report: tests added, coverage before and after, mutation score, and every surviving mutant with
an explanation of why it survived.
