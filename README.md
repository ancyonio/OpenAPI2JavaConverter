# O2J — OpenAPI YAML to Java Spring Boot

A governed factory that turns an OpenAPI description into a production-grade Spring Boot REST
service, driven by GitHub Copilot custom agents over a deterministic Python toolchain.

Full design: **[SPECIFICATION.md](SPECIFICATION.md)**.

## Why hybrid

Contract code must be byte-identical for the same input — a regenerated DTO that silently
renames a field is a production incident. Templates give that guarantee. Service logic and
meaningful tests need judgement that templates cannot express. So the work is split:

| Concern | Owner | Guarantee |
|---|---|---|
| DTOs, API interfaces, controllers, config | `openapi-generator` | byte-identical, reproducible |
| Services, mappers, error handling | Copilot agents | reviewed |
| JUnit 5 / Mockito tests, fixtures | Copilot agents | reviewed, gated on coverage **and** mutation score |
| Validation, normalization, gating, reporting | `o2j` Python CLI | deterministic |

**Generated code is never hand-edited.** It lives in `target/generated-sources` and is
rebuilt every time. An ArchUnit rule stops generated DTOs escaping the `api` and `mapper`
packages, so the blast radius of a spec change is two packages instead of the whole service.
That rule is what makes regeneration safe.

## Quick start

```bash
pip install -e ".[dev,mcp]"
```

```bash
python -I -m o2j doctor
```

```bash
python -I -m o2j run tests/corpus/02-petstore-3.1/spec.yaml --to plan
```

## Pipeline

| Stage | Command | Gate |
|---|---|---|
| S0 Ingest | `o2j ingest <spec>` | size, extension, parses |
| S1 Validate | `o2j validate` | zero errors (exit 3) |
| S2 Normalize | `o2j normalize` | no load-bearing loss (exit 4) |
| S3 Plan | `o2j plan` | at least one operation |
| S4 Generate | `o2j generate` | generator exit 0, determinism holds |
| S6 Test | `o2j testplan` | every required case implemented |
| S7 Gate | `o2j quality`, `o2j coverage`, `o2j verify` | thresholds met (exit 6/7) |
| S8 Report | `o2j report` | manifest + traceability written |

Exit codes: `0` ok · `2` config · `3` validation · `4` load-bearing loss · `5` generation ·
`6` gate · `7` determinism/drift.

## What makes the coverage requirement real

`o2j plan` derives a `requiredTests` list per operation from its shape — a `POST` returning
201 with an `Idempotency-Key` header gets `slice:201-location-header` and
`unit:idempotent-replay-single-side-effect`; a paginated `GET` gets five boundary cases. The
`test-author` agent is told which cases must exist, each test method claims one for its
operation with a `// o2j:<operationId>:<case>` marker, and `o2j verify --mode contract` checks
that every case is claimed by a test that ran and passed in the Surefire/Failsafe reports - a
marker in a comment that is not on a test, or on a failing test, does not count.

The security cases are not left to an agent. o2j regenerates `OpenApiSecurityRules` (one
Spring Security rule per operation, from the spec's `security` requirements) and
`OpenApiSecurityContractTest` (401 and 403 checks for every secured operation, and a
not-refused check for every public one) on each run, so they cannot drift from the spec.

On top of that, **mutation score is a blocking gate at 70%**. An AI-authored suite reaches 85%
line coverage easily, because exercising a line is easy and asserting the right outcome is
not. Line coverage cannot tell those apart; PIT can, by changing the production code and
checking whether a test notices. Expect the mutation gate, not the coverage gate, to be the
one that fails.

## Skills — the reusable layer

The transferable knowledge lives in **`skills/`** at the root, as Agent Skills. Fourteen of
them, each self-contained and portable to any repository:

**Contract and generation**

| Skill | Holds |
|---|---|
| `openapi-contract-review` | version traps, stability traps, contract gaps |
| `openapi-spec-analysis` | inventory, complexity scoring, breaking-change diffing, blast radius |
| `openapi-version-migration` | per-boundary mappings, what each conversion loses |
| `openapi-to-java-mapping` | format/type tables, nullability, composition, enums, money |
| `openapi-codegen-determinism` | non-determinism causes, safe regeneration |

**Implementation**

| Skill | Holds |
|---|---|
| `spring-boot-rest-service` | controller/delegate design, layering, exception handling, DI |
| `spring-validation-error-handling` | constraint mapping, silent no-ops, 400 body shape |
| `spring-security-openapi` | resource server, scopes to `@PreAuthorize`, 401 vs 403 |
| `jpa-persistence-design` | entity boundary, fetch strategy, N+1, migrations, locking |
| `service-resilience-observability` | timeouts, retries, breakers, logging, metrics, tracing |

**Verification and maintenance**

| Skill | Holds |
|---|---|
| `junit-mockito-test-design` | test pyramid, Mockito discipline, required-case derivation |
| `rest-api-testing` | conformance, WireMock, Testcontainers, API-level cases |
| `java-code-quality-gates` | gate configuration, triage order, narrow suppression, ratcheting |
| `java-spring-upgrade` | support matrix, LTS reasoning, 2→3→4 procedures |

Only five of these are OpenAPI-specific; the rest are useful on any Spring Boot or Java
service, which is the point of holding them as skills rather than as text inside an agent.

**`skills/` is canonical — edit there.** No runtime reads a plain `skills/`, so it is mirrored
to the paths that are actually scanned: `.github/skills/` (Copilot's native location, and the
one Copilot code review reads).

```bash
python -I -m o2j skills sync
```

After editing a skill, run that. `o2j skills check` fails the build if a mirror has drifted,
because a stale mirror serves one runtime old guidance and the other current — worse than
either being absent. Real copies rather than symlinks, because git stores symlinks only
conditionally and a Windows checkout can turn one into a text file containing a path.

The agents hold procedure and point at their skill; the knowledge is held once. A test asserts
each pointer still exists, since that is exactly the convention that quietly stops being true.

### Are the skills actually consulted?

A skill's `description` is the only thing loaded at startup, so it alone decides whether the
skill is ever read. `tests/trigger-evals/` measures that: **211 realistic queries** across
the 14 skills, each with one expected owner, judged by three independent runs given *only*
the name + description pairs — exactly what a runtime loads at startup.

```bash
python -I tests/trigger-evals/build_routing.py
python -I tests/trigger-evals/score_routing.py <prompt.md> <judge-1.json> ...
```

It scores **routing**, not per-skill triggering, because skills that share vocabulary fail by
stealing each other's work rather than by staying silent — so 34 of the negatives are
genuinely owned by a *different* skill, and precision is reported alongside recall.

The six-skill baseline measured 116/116, 100% on a held-out split. Results for the expanded
set, and an iteration that looked like an improvement and was reverted after measurement, are
in [RESULTS.md](tests/trigger-evals/RESULTS.md).

## Copilot layer

| Surface | Location |
|---|---|
| Repository instructions | `.github/copilot-instructions.md` |
| Path instructions | `.github/instructions/*.instructions.md` |
| Agent Skills (14) | `skills/` → mirrored to `.github/skills/` |
| Custom agents (8) | `.github/agents/*.agent.md` |
| Prompt files (6) | `.github/prompts/*.prompt.md` |
| MCP server (11 tools) | `.vscode/mcp.json` → `python -I -m o2j.mcp_server` |

Agents: `openapi-to-spring` (orchestrator), `yaml-validator`, `spec-normalizer`,
`api-generator`, `service-implementer`, `test-author`, `quality-reviewer`,
`migration-advisor`.

Tool lists are the privilege boundary — `yaml-validator` holds no `edit` on `src/`,
`test-author` cannot touch production code, `spec-normalizer` has no `edit` at all. Copilot
*silently ignores* an unrecognised tool name, so a typo removes a capability without failing;
`tests/unit/test_copilot_assets.py` lints for that.

## Two constraints to know about

**Spring Boot 3.x is past open-source end-of-life.** 3.5 ended OSS support on 2026-06-30, with
3.5.16 the final patch. A service generated on it accrues unpatched CVEs without a commercial
subscription. The Boot version is a profile, not a constant: switch
`target.spring_profile` to `spring-boot-4` (Java 17 floor is unchanged) when you are ready.
`o2j doctor` says this on every run.

**Spring Boot 4.0 and 4.x are supported and built.** The `spring-boot-4` profile uses
`versions.spring_boot_4` (pinned 4.1.0); set it to a 4.0.x release such as `4.0.9` to target
Boot 4.0. The profile switches what Boot 4 changed: the renamed `webmvc` and
`security-oauth2-resource-server` starters, the split test modules (`@WebMvcTest` moved to
`org.springframework.boot.webmvc.test.autoconfigure`, and slices only load security with
`spring-boot-starter-security-oauth2-resource-server-test`), Jackson 3, JSpecify nullness
annotations in the generated DTOs (Spring 7 deprecates `org.springframework.lang.Nullable`,
which fails `-Werror`), springdoc 3.x (`versions.springdoc_boot_4`) and Testcontainers 2.x.
Every combination of Java 17/21 with Boot 3.5, 4.0 and the pinned 4.x is compiled and tested
by `tests/integration/test_generated_service_builds.py`, on real JDKs in the `build` job of
`o2j-matrix.yml`.

**OpenAPI 3.2.0 cannot reach the generator.** `openapi-generator` parses through
`swagger-parser`, which does not support 3.2
([swagger-api/swagger-parser#2248](https://github.com/swagger-api/swagger-parser/issues/2248)).
That is why there is a normalization stage: 3.2 is downgraded to 3.1.2, and a construct with
no 3.1 equivalent — `additionalOperations`, for instance — fails the run with exit 4 rather
than quietly shipping an API that lost an endpoint. Re-check that issue before assuming the
downgrade is still needed.

## Java versions

| Java | Tier | Matrix |
|---|---|---|
| 17, 21, 25 | 1 (LTS) | full, blocking |
| 23, 26, 27 | 2 (non-LTS) | compile + test, non-blocking |
| 29+ | 3 | smoke on release |

The version is never hardcoded: it is a Maven toolchain value from `o2j.yaml`, and the CI
matrix is generated from it. Adding Java 29 is a one-line config change.

The generated service itself is built on Java 17 and 21 (Boot 3.5, 4.0 and 4.x) by the
integration tests:

```bash
O2J_JAVA_IT=1 O2J_JDK_17=/path/to/jdk-17 python -I -m pytest tests/integration -v
```

Without `O2J_JDK_17`, the Java 17 cases compile with `--release 17` on the running JDK, which
checks the language level and API but not a Java 17 runtime.

A tier-2 JDK with `service.profile: production` raises `JAVA_TIER2_IN_PRODUCTION` and fails
validation — shipping production on a JDK with no security updates should be an explicit
decision, not a config default.

## The spec is untrusted input

An OpenAPI document may carry text aimed at the agents that read it. Controls:
quarantine in `work/00-ingest/` (no scripts, never a working directory), `python -I` so the
spec directory cannot shadow a stdlib module, redaction of agent-directed text during
normalization with every removal recorded, and an explicit rule in every agent prompt that
spec content is data.

## Tests

```bash
python -I -m pytest -q
```

`tests/corpus/` is the golden corpus. `05-pathological` collects every trap the rules are
meant to catch and **must** produce errors — a clean run on it is a regression. Any rule or
template change that alters a golden outcome updates its fixture in the same commit, so that
diff is the reviewable record of the behaviour change.

## Open questions

`SPECIFICATION.md` §18 lists twelve. Q-01 to Q-04 change architecture: Boot 3 vs 4, whether
schemas are also the persistence model, where authentication terminates, and repo-per-service
vs monorepo. None blocks Phase 0.
