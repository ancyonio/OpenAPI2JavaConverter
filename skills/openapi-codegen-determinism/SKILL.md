---
name: openapi-codegen-determinism
description: >
  Make generated code reproducible and safe to regenerate — eliminating non-deterministic
  output, isolating generated types so a schema change cannot ripple through business logic,
  and handling regeneration over code that humans have already edited. Use this whenever you
  set up or debug a code generator (openapi-generator, protobuf, GraphQL codegen, any
  template-driven generator), whenever regenerating produces a large diff with no real change,
  whenever generated files are committed to a repository and you are deciding whether they
  should be, and whenever someone is nervous about re-running a generator because of what it
  might overwrite. Reach for it before the first generated file lands, because the decisions
  here are very expensive to reverse once a team has built on them.
license: Apache-2.0
compatibility: >
  Generator-agnostic. Examples use openapi-generator with the Java/Spring generator; the
  principles apply to any template-driven generator.
---

# Making generated code reproducible and safe to regenerate

Code generation fails in two distinct ways, and conflating them is why so many teams end up
generating once and then hand-maintaining the output forever.

**Non-determinism**: the same input produces different output. The generated tree cannot be
treated as build output, so every regeneration becomes a diff review, and nobody can tell a
real change from noise.

**Unsafe regeneration**: re-running the generator destroys or silently breaks hand-written
code. So people stop re-running it, the spec becomes documentation rather than the source of
truth, and the two drift.

Solve both and the generator becomes part of the build. Solve neither and it is a one-time
scaffolding tool.

## Determinism

### Find the usual suspects first

Almost every non-determinism is one of these:

| Cause | Fix |
|---|---|
| A generation timestamp in file headers | Turn it off. In openapi-generator: `hideGenerationTimestamp=true`. This is non-negotiable — nothing else you do matters while every file's header changes every run. |
| Unordered map iteration in templates | Sort keys when preparing the input. Hash-map order is stable within a JVM run and not across versions or platforms. |
| Ordinal names for anonymous types | Derive names from the input instead. `InlineObject1` renumbers when the document is edited — see below. |
| Locale- or timezone-dependent formatting | Pin `LC_ALL=C` and `TZ=UTC` in CI. Number formatting and case conversion are locale-sensitive. |
| YAML anchor/alias expansion order | Strip anchors during canonicalisation. |
| Floating transitive dependency versions | Pin every version. A generator minor bump can change output. |
| Absolute paths baked into output | Use paths relative to the project root. |

### Ordinal naming is the subtle one

An anonymous schema becomes `InlineObject1`, `InlineObject2`, numbered by document order. The
output is deterministic for a *given* input, so a naive determinism check passes. But insert an
unrelated operation above and every later number shifts, silently renaming generated classes
and breaking every import.

The fix is upstream of the generator: hoist anonymous types into named components, with names
derived from their context (`CreateOrderRequest`, `OrderShippingAddress`), before generating.
Then a name is a function of what the thing *is*, not where it sits in the file.

If two derived names collide, disambiguate with a letter suffix rather than a number — a number
reads as an ordinal and invites exactly the confusion you removed.

### Verify it, do not assume it

Determinism is not something you reason your way to. Generate twice and diff:

```bash
generate --output build/run-a
generate --output build/run-b
diff -r build/run-a build/run-b
```

Make this a blocking CI check on every push. It is cheap, and it is the only thing that stops
a template change from quietly reintroducing non-determinism.

When it fails, diff the two trees and look at the *first* differing line — the cause is usually
obvious from it, and chasing a later difference wastes time.

### Hash the input, record it

Serialise the generator's input canonically (stable key order, fixed indentation, LF endings)
and hash it. That hash is the identity of the generated code. Record it in a lock file
alongside the generator version and the target settings, and you can answer "which spec
produced this code?" months later — which is the question that actually comes up.

Exclude the lock file's own timestamp from its content hash. Otherwise the determinism check
fails on the artefact it just wrote.

## Safe regeneration

### Generated code is build output

Generate into `target/`, `build/` or equivalent, and git-ignore it. Never into `src/`.

This single decision prevents most regeneration problems, because it removes the question of
whether a generated file has been edited — it cannot have been, it did not exist until the
build ran. The corollary is a rule worth stating explicitly to every contributor, human or
otherwise: **to change generated output, change the input or the template, never the output.**

Teams sometimes commit generated code so that IDEs and reviewers can see it. If you must, keep
it in a clearly separated directory, mark it as generated (`.gitattributes` with
`linguist-generated=true` collapses it in diffs), and keep the determinism check — otherwise
every unrelated PR carries a wall of generated churn.

### Isolate generated types behind a boundary

This is the rule that makes regeneration genuinely safe rather than merely non-destructive.

Generated types must not appear in signatures outside a thin boundary layer. In a service with
a generated API: generated DTOs live in `api` and `mapper` only. Services, domain objects and
repositories never import them.

The reasoning: a renamed field in the spec is a routine upstream change. If DTOs are spread
through the codebase, that rename ripples into every service and every test, and regeneration
becomes a refactor. Confined to two packages, the blast radius is bounded and the rename is a
twenty-minute change.

Enforce it mechanically — ArchUnit for Java, an import-boundary lint rule elsewhere. This is
precisely the rule that erodes one pragmatic exception at a time, and each exception is
individually defensible.

### Make silent data loss a compile error

The worst outcome of a schema change is not a build failure. It is a mapper that keeps
compiling and silently stops copying a renamed field.

Configure the mapping layer strictly so that an unmapped target is an error — in MapStruct,
`unmappedTargetPolicy = ERROR`. Then a renamed field fails the build at the mapper, which is
the cheapest possible place to discover it.

Expect pushback the first time it fires during a regeneration. The answer is that this is the
mechanism working: the alternative was shipping a field that silently became null.

### Scaffold hand-written files once, never overwrite

Generators often also scaffold the files you are meant to fill in. Those must be write-once:

- If the target file does not exist, write it.
- If it exists and the content is identical, do nothing.
- If it exists and differs, write `<name>.new` beside it and **report a conflict** for human
  merge.

Never overwrite. A generator that clobbers a hand-written service implementation teaches the
team never to run it again, and they will be right.

### Report the regeneration delta

When the input has changed and code already exists, say what changed in terms of the code:

- operations added — needs a new delegate, service method, tests
- operations removed — hand-written code is now orphaned; **report it, do not delete it**
- request/response types changed — the mapper will fail to compile, deliberately
- fields newly required — new validation-failure test cases needed
- `operationId` renamed — breaking; needs explicit approval

Deleting hand-written code for a removed operation is the generator making a product decision.
Report it and let a human decide; the operation may be coming back next sprint.

## Setting up a generator well

A checklist that saves most of the pain:

- [ ] Timestamps off.
- [ ] Output under `target/`/`build/`, git-ignored.
- [ ] Anonymous types hoisted and named before generation.
- [ ] Input canonicalised and hashed; hash recorded in a lock file.
- [ ] Generated test stubs **off**. Empty generated tests assert nothing while inflating the
      coverage denominator, which makes a coverage gate meaningless.
- [ ] Generated API docs off unless you publish them — they are large and churn.
- [ ] Boundary isolation rule in place and enforced by a test.
- [ ] Mapping layer strict.
- [ ] Double-generate-and-diff as a blocking CI check.
- [ ] `LC_ALL=C`, `TZ=UTC` in CI.
- [ ] Every version pinned, generator included.
- [ ] Scaffolding is write-once with conflict reporting.

## Template overrides

When the generator's default output is wrong for your project, override the template rather
than post-processing the output. A post-processing step is another place for non-determinism to
enter, and it breaks silently when the generator's output format shifts.

Keep overrides minimal and diffed against the generator's originals, so a generator upgrade
shows you exactly what you are now out of step with. An override copied wholesale from an old
version is how a project gets stuck three majors behind.
