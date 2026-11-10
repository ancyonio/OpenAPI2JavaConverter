# ADR 0001 — Hybrid generation: templates for the contract, agents for the rest

- **Status:** Accepted
- **Date:** 2026-11-09
- **Supersedes:** —

## Context

An OpenAPI-to-Spring-Boot factory can be built three ways: deterministic templates only,
Copilot agents only, or a split.

Template-only generation is reproducible but cannot write business logic or meaningful tests;
what it produces for those is boilerplate that a human rewrites immediately.

Agent-only generation writes idiomatic code but is non-deterministic. The same spec can yield
different DTO field names on two runs. That is not a quality concern, it is a correctness one:
a regenerated DTO that silently renames a field is a production incident, and nothing in the
build would catch it.

## Decision

Split by whether the work needs determinism or judgement.

| Concern | Owner |
|---|---|
| DTOs, API interfaces, controllers, configuration | `openapi-generator` templates |
| Services, mappers, domain model, error handling | Copilot agents |
| JUnit 5 / Mockito tests | Copilot agents |
| Validation, normalization, gating, reporting | `o2j` Python CLI |

Two constraints make the split hold:

1. **Generated code is never hand-edited.** It lives in `target/generated-sources` and is
   rebuilt every time. To change it, change the spec or the Mustache template.
2. **Generated DTOs may not leave the `api` and `mapper` packages**, enforced by the ArchUnit
   rule `generated_dtos_do_not_escape_the_api_layer`.

## Consequences

**Good.** The blast radius of a spec change is two packages instead of the whole service.
Regeneration is safe, so the spec stays authoritative rather than drifting into documentation.
The determinism gate (`o2j verify --determinism`) is meaningful because the reproducible half
is genuinely reproducible.

**Costs.** A mapping layer exists that a DTO-everywhere design would not need, and MapStruct
with `unmappedTargetPolicy = ERROR` turns a renamed spec field into a compile error. That is
deliberate: a compile error is the cheapest possible place to discover that a spec change
would have silently dropped a field.

**Rejected alternative.** Letting services take generated DTOs directly. It removes the mapper
layer, and with it the isolation that makes regeneration safe — every spec change would then
ripple through business logic and every test.
