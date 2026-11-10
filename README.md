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
| Contract tests: security, validation, error mapping, page limits, EMS bridge | `o2j` (regenerated) | derived from the spec, always current |
| Business-rule tests (JUnit 5 / Mockito) | Copilot agents, from o2j skeletons | reviewed, gated on coverage **and** mutation score |
| Response conformance | `o2j verify --mode conformance` | every recorded response checked against the spec |
| Validation, normalization, gating, reporting | `o2j` Python CLI | deterministic |

**Generated code is never hand-edited.** It lives in each service's
`generated_Code/<service>/target/generated-sources` and is rebuilt every time. An ArchUnit rule
stops generated DTOs escaping the `api` and `mapper` packages, so the blast radius of a spec
change is two packages instead of the whole service. That rule is what makes regeneration safe.

## Folders: input and output

| Folder | Role | Commit it? |
|---|---|---|
| `openapi/` | **Input.** Put the OpenAPI specs to convert here, one `*.yaml` or `*.yml` per service. | yes |
| `generated_Code/<service>/` | **Output.** One self-contained Maven project per spec: `pom.xml`, `src/main/java`, `src/test/java`, `src/main/resources`, and `openapi/spec.canonical.yaml` (the exact spec the code was built from). | yes, except its `target/` |
| `generated_Code/<service>/target/generated-sources/openapi` | DTOs, `*Api` interfaces and controllers from `openapi-generator`, rebuilt on every build. | no (git-ignored) |
| `work/` | Stage artifacts: quarantined spec, reports, plan, manifest. In folder runs, `work/<service>/`. | no (git-ignored) |

Both folders are set in `o2j.yaml`: `openapi.input` (a folder or a single file) and
`generation.service_root`.

How the service folder is named:

- **Folder run** (`o2j run` or `o2j run openapi`): every spec becomes its own service, named
  after the file. `openapi/orders.yaml` becomes `generated_Code/orders/` and
  `openapi/Customer API.v2.yml` becomes `generated_Code/customer-api-v2/`. Two specs that map
  to the same name stop the run before anything is written.
- **Single spec** (`o2j run openapi/orders.yaml`, or the stage-by-stage commands below): the
  service is `generated_Code/<service.artifact_id>/`, from `o2j.yaml`. Set `artifact_id` to the
  spec's name (`orders`) so both ways of running write to the same folder.

## Quick start

```bash
pip install -e ".[dev,mcp]"
```

```bash
python -I -m o2j doctor
```

Copy a sample spec into `openapi/` and convert everything in the folder up to the plan:

```bash
cp tests/corpus/02-petstore-3.1/spec.yaml openapi/petstore.yaml
python -I -m o2j run --to plan
```

## Step by step: OpenAPI YAML → Spring Boot REST endpoints

This walkthrough takes one spec, `openapi/orders.yaml`, through each stage by hand and ends
with a running service in `generated_Code/orders/`. To convert every spec in `openapi/` in one
command, skip to [All at once](#all-at-once). On a failed gate, every command stops with the
exit code listed under [Pipeline](#pipeline).

### 1. Install the prerequisites

| Tool | Needed for |
|---|---|
| Python 3.11+ | the `o2j` CLI |
| JDK 17 / 21 / 25 | building the generated service (must match `target.java_version`) |
| Maven 3.9+ | running `openapi-generator` and the build gates |
| Node.js (`npx`) or `spectral` | house API-standard linting (optional) |
| `gitleaks` | the secret-scanning gate (optional) |

```bash
pip install -e ".[dev,mcp]"
```

### 2. Put the spec in `openapi/` and configure the service

```bash
cp /path/to/orders.yaml openapi/orders.yaml
```

Then edit `o2j.yaml`:

- `service.name`, `service.artifact_id`: the service name, which is also its folder under
  `generated_Code/`. Use the spec's file name (`orders`).
- `service.group_id`, `service.base_package`: Maven coordinates and Java package.
- `target.java_version` and `target.spring_profile` (`spring-boot-3` or `spring-boot-4`).
- `openapi.input`: leave it as `openapi` to read the folder, or name a single file.
- `generation.service_root`: where services are written (default `generated_Code`).
- `quality.mutation.target_packages`: set this to your `<base_package>.service.*`.
- `ems`: only if you are replacing TIBCO EMS queues (see [below](#replacing-tibco-ems-queues)).

To keep specs and services in another directory, run `o2j init --service orders
--base-package com.acme.orders --java 21` there. It writes a fresh `o2j.yaml`, and `openapi/`,
`generated_Code/` and `work/` are then resolved from that directory.

### 3. Check the toolchain

```bash
python -I -m o2j doctor
```

Any `MISS` line prints a remedy. Fix those before going on. Add `--check-versions` to see
whether the pinned versions in `o2j.yaml` are behind Maven Central.

### 4. Ingest the spec (S0)

```bash
python -I -m o2j ingest openapi/orders.yaml
```

Copies the spec into quarantine (`work/00-ingest/`), checks its size and extension, records
its hash and detects the version (Swagger 2.0, OpenAPI 3.0, 3.1 or 3.2). With exactly one spec
in `openapi/`, the path can be left out. With several, the stage commands need it, because they
never guess which one you meant.

### 5. Validate it (S1)

```bash
python -I -m o2j validate openapi/orders.yaml
```

Checks syntax, semantics and the house rules: security on every operation, pagination on
collections, Problem Details error responses, naming. Each finding prints a `fix:` hint, and a
SARIF report is written to `work/01-validate/` for code-scanning UIs. **Fix the spec in
`openapi/`, not the generated code.** Exit `3` means at least one error remains.

### 6. Normalize to the canonical version (S2)

```bash
python -I -m o2j normalize openapi/orders.yaml
```

Converts the spec to OpenAPI 3.1.2, the version the Java toolchain accepts. Swagger 2.0 and 3.0
are upgraded. 3.2 is downgraded, and anything with no 3.1 equivalent fails with exit `4`.
Text aimed at agents is redacted and each removal is recorded. The output is the canonical
spec path and its hash, which every later stage builds from.

### 7. Plan the endpoints and required tests (S3)

```bash
python -I -m o2j plan
python -I -m o2j testplan
```

`plan` lists every operation and resource, a risk score with its drivers, and the test cases
each operation's shape requires. `testplan` prints those cases as the `// o2j:<operationId>:<case>`
markers the tests must carry. Review a `high` risk band before building logic on the model.

### 8. Generate the Spring Boot code (S4)

```bash
python -I -m o2j generate
```

Runs `openapi-generator` through Maven and scaffolds the rest, all into `generated_Code/orders/`
(`--output <dir>` writes somewhere else). Layout:

| Path under `generated_Code/orders/` | Contents | Who owns it |
|---|---|---|
| `pom.xml` | build, with every version from `o2j.yaml` | o2j |
| `openapi/spec.canonical.yaml` | the spec this code was built from | o2j: never edit |
| `target/generated-sources/openapi` | DTOs, `*Api` interfaces, `*ApiController` | generator: never edit |
| `src/main/java/<base_package>/api/` | `*ApiDelegateImpl`: one method per endpoint | you |
| `src/main/java/<base_package>/service/` | business services the delegates call | you |
| `src/main/java/<base_package>/mapper/` | MapStruct mappers between DTOs and domain types | you |
| `src/main/java/<base_package>/config/` | `SecurityConfig`, `OpenApiSecurityRules`, correlation-id filter | o2j |
| `src/main/java/<base_package>/error/` | `GlobalExceptionHandler` (RFC 9457 Problem Details), domain exceptions | o2j |
| `src/test/java/<base_package>/` | contract tests (regenerated) and business-test skeletons (yours) | both |

Contract tests are regenerated on every run, and `@Disabled` skeletons are created for the
business cases. If a scaffolded file you changed would be overwritten, o2j writes
`<file>.o2j-new` and reports a `CONFLICT` for you to merge by hand. Use `--no-run-generator`
to scaffold without Maven.

Confirm that regenerating gives byte-identical output:

```bash
python -I -m o2j verify --mode determinism
```

### 9. Implement the endpoints

The generated controller handles routing, binding and validation, then calls your delegate. For
each operation, in `generated_Code/orders/src/main/java/<base_package>/`:

1. Implement the method in `api/<Resource>ApiDelegateImpl`: map the request DTO with the
   mapper, call the service, map the result back and return a `ResponseEntity` with the status
   the spec declares.
2. Put the business rules in `service/<Resource>Service`. Throw `NotFoundException` or
   `ConflictException` and the handler turns them into 404 and 409 Problem Details.
3. Keep generated DTOs inside `api` and `mapper`. `ArchitectureTest` fails the build if they
   leak further.

In VS Code with Copilot, the `service-implementer` agent does this step. The
`/convert-openapi` prompt runs the whole pipeline through the `openapi-to-spring` orchestrator.

### 10. Write the business tests

Fill in the `@Disabled` methods in `<Service>Test` (Mockito) and `<Api>BehaviourTest` (web
slice) under `generated_Code/orders/src/test/java`, then remove `@Disabled`. Keep each method's
`// o2j:` marker. The `test-author` agent can do this.

```bash
python -I -m o2j verify --mode contract --static   # markers are on test methods
```

### 11. Build and run the gates (S7)

```bash
mvn -f generated_Code/orders/pom.xml verify
python -I -m o2j quality                        # Spotless, Checkstyle, PMD, SpotBugs, ArchUnit, OWASP, gitleaks
python -I -m o2j coverage --no-run-build        # line, branch, service-layer and mutation thresholds
python -I -m o2j verify --mode contract         # every required case claimed by a passing test
python -I -m o2j verify --mode conformance      # every recorded response matches the spec
```

The o2j commands run against `generated_Code/<service.artifact_id>/`. The default thresholds
are 85% line, 80% branch, 90% service-layer line and 70% mutation score, all set in `o2j.yaml`.
Exit `6` means a gate failed.

### 12. Report and run the service (S8)

```bash
python -I -m o2j report
cd generated_Code/orders
JWT_ISSUER_URI=https://your-idp/realms/main mvn spring-boot:run
```

`report` writes the manifest, the traceability matrix (operation → code → tests) and a PR body
to `work/08-report/`, and `o2j.lock.json` into the service folder. The service starts on port
8080. springdoc serves the live contract at `/v3/api-docs` and Swagger UI at
`/swagger-ui.html`. Secured endpoints need a JWT from the issuer in `JWT_ISSUER_URI`. Without
that variable the service rejects every token, so it fails closed.

### All at once

`o2j run` runs any range of stages and stops at the first failed gate. With no argument it
reads `openapi.input`, which is the `openapi/` folder, and converts **every** `*.yaml` /
`*.yml` in it:

```bash
python -I -m o2j run                                    # every spec in openapi/, ingest -> report
python -I -m o2j run --to generate                      # stop after codegen
python -I -m o2j run --only orders --from gate          # re-gate one service after implementing it
python -I -m o2j run --java 17                          # override the Java target
python -I -m o2j run openapi/orders.yaml                # one file: service name from o2j.yaml
```

A folder run writes each service to `generated_Code/<spec-name>/` and its stage artifacts to
`work/<spec-name>/`. A spec that fails a gate does not stop the others. The run ends with a
per-service summary and exits with the highest exit code. `--only` takes a file name
(`orders.yaml`), a file stem or a service name, and can be repeated. EMS routes name the
operations of one spec, so they are skipped when a folder run converts more than one.

Stages: `ingest`, `validate`, `normalize`, `plan`, `generate`, `gate`, `report`.

### When the spec changes

Edit the spec in `openapi/` and re-run from `ingest`. Generated code and contract tests in
`generated_Code/<service>/` are rebuilt. Scaffolds you already edited are kept, and any that
conflict show up as `.o2j-new`. Use `python -I -m o2j verify --mode drift` to check whether the
code still matches the spec, and set `validation.breaking_change_baseline` to the previous spec
so breaking changes are flagged during validation. Renaming a spec file renames its service
folder in folder runs, so move `generated_Code/<old-name>/` along with it.

## Pipeline

| Stage | Command | Gate |
|---|---|---|
| S0 Ingest | `o2j ingest openapi/<spec>.yaml` | size, extension, parses |
| S1 Validate | `o2j validate` | zero errors (exit 3) |
| S2 Normalize | `o2j normalize` | no load-bearing loss (exit 4) |
| S3 Plan | `o2j plan` | at least one operation |
| S4 Generate | `o2j generate` → `generated_Code/<service>/` | generator exit 0, determinism holds |
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

### Generated tests, and what is left to people

o2j writes every required case the spec fully determines, into files it regenerates on each
run so they cannot drift:

- `OpenApiSecurityContractTest`: 401 and 403 for every secured operation; public ones are
  not refused.
- `<Api>ContractTest`: one `@WebMvcTest` per resource, with the delegate replaced by a stub.
  A valid request gets the declared status and content type, and reaches the delegate once.
  Each of these is refused with 400 Problem Details *before* the delegate runs: a body
  missing a required field, malformed JSON, an unknown enum value, a missing required
  parameter, a wrongly-typed one, a page size over its maximum. A `NotFoundException`
  becomes 404. Requests are synthesized from the schemas and validated against them first;
  when no valid sample can be built, the case is left for a person rather than guessed.
- `EmsBridgeContractTest`, when EMS queues are mapped (below).

What needs business knowledge - the rule behind `unit:happy-path`, what an empty or last page
holds, which duplicate is a 409, the value of a `Location` header - is scaffolded once as
`@Disabled` methods in `<Service>Test` (Mockito) and `<Api>BehaviourTest` (slice, real
delegate and mapper, mocked service), each already carrying its marker. The contract gate keeps
counting them as missing until someone writes them and removes `@Disabled`. `o2j generate`
prints the split, for example *34 required cases generated, 20 left as skeletons* for the 3.1
petstore with two EMS queues.

### Response conformance

A test that passes says nothing about whether the response matched the contract: a slice test
checking the status passes even when the body is missing a required field. Every generated
service has a test-scope `O2jConformanceRecorder` filter that records each HTTP exchange any
MockMvc or `@SpringBootTest` test produces, and `o2j verify --mode conformance` checks each
one against the spec: status declared, content type declared, body valid against the JSON
Schema (formats included). `contract:response-schema` only counts for an operation when its
recorded 2xx responses conform. The more real behaviour the suite exercises, the more of the
service's real output is checked.

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

## Replacing TIBCO EMS queues

The REST service replaces EMS request/response queues, but their consumers do not all move on
the same day. Map the legacy queues in `o2j.yaml` (see the commented example there):

```yaml
ems:
  enabled: true
  routes:
    - destination: ORDERS.CREATE.REQUEST
      operation: createOrder
      correlation: message-id          # reply JMSCorrelationID = request JMSMessageID
      params: { Idempotency-Key: "jms:JMSMessageID" }
      faults: { 409: ORD-DUPLICATE }   # HTTP status -> fault code consumers already handle
```

`o2j plan` validates the mapping against the spec and fails the run (exit 2) on an unknown
operation or parameter, a fault status the operation never returns, a bad parameter source or
a duplicated queue. Parameter sources: `property:<name>` (default: the parameter name with
non-identifier characters as `_`), `jms:JMSMessageID`, `jms:JMSCorrelationID`,
`json:/<pointer>` into the message body, `literal:<value>`.

o2j then generates `EmsRoutes` (the validated table) and `EmsBridge`, which listens on each
queue, makes the HTTP call the spec describes **through the service's own HTTP stack** - so
security, validation, error mapping and correlation ids apply exactly as for a direct caller -
and replies on `JMSReplyTo` with the response body, the configured correlation, `o2j_status`,
and for errors `o2j_fault` (the mapped code, `HTTP_<status>`, `TIMEOUT` past the reply
deadline, `UNAVAILABLE`). Expired requests are dropped as EMS would; failures inside the bridge
still reply, so no requester is left waiting. `EmsBridgeContractTest` proves each route end to
end on an embedded Artemis broker: correlated reply, every mapped fault, the timeout.

The bridge is enabled per deployment (`O2J_EMS_ENABLED=true`) and is removed when the last
consumer has moved to HTTP. In production it needs TIBCO's Jakarta JMS client and a
`TibjmsConnectionFactory` bean: the client is not on Maven Central, so it comes from your
internal repository. `EmsBridgeCredentials` (yours) supplies the bridge's bearer token: replace
the static token with a client-credentials flow before production. Payloads are JSON; XML
messages need a transformation step that this bridge does not provide.

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
