---
mode: agent
agent: migration-advisor
description: Upgrade the Java or Spring Boot version with an impact report.
---

Upgrade this service to `${input:target}` (for example "Java 25" or "Spring Boot 4").

Confirm current version facts first with `o2j doctor --check-versions` — the recorded facts in
this repository carry an "as of" date and may be stale. State the date of what you report.

Change `o2j.yaml` only; never edit a POM directly. Then regenerate, build and test on the new
toolchain, and run `o2j_verify --determinism` and `--contract`.

If the target is a non-LTS Java version (23, 26, 27) and `service.profile` is `production`,
stop and explain the exposure before doing anything.

Report: blocking incompatibilities, required code changes by file, effort estimate, features now
available that you deliberately did not adopt, and residual risk.
