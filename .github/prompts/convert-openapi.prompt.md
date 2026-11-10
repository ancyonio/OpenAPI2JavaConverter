---
mode: agent
agent: openapi-to-spring
description: Convert an OpenAPI YAML specification into a Spring Boot service end to end.
---

Convert the OpenAPI specification at `${input:specPath}` (a spec under `openapi/`) into a
Spring Boot service. Write the service to `generated_Code/<service>/`, where `<service>` is
`service.artifact_id` from `o2j.yaml`. To convert every spec in `openapi/` at once, use
`python -I -m o2j run openapi` instead: it writes one service per spec, named after the file.

Target: Java `${input:javaVersion:21}`, Spring Boot 3.x, Maven, Spring Web MVC.
Base package: `${input:basePackage}`.

Run the full O2J pipeline in order, enforcing every stage gate. Stop at the first failed gate
and report rather than working around it.

Report at the end:
- operations converted, and any skipped with the reason
- files created, by layer
- line coverage, branch coverage, mutation score
- gate status for each quality tool
- open findings needing a human decision
- the canonical spec hash this code was built from
