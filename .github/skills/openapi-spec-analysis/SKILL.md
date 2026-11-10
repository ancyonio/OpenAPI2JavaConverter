---
name: openapi-spec-analysis
description: >
  Answer questions *about* an API description rather than finding defects in it — how big it
  is, how complex, what it would cost to implement, what changed between two versions and
  whether that change breaks clients, which operations are risky, and what a spec change's
  blast radius is on existing code. Use this whenever you need to size or estimate work from a
  spec, compare two versions of one, assess whether a diff is backward compatible, produce an
  operation inventory or coverage report, prioritise which endpoints to build or test first, or
  explain a large unfamiliar spec to someone. Reach for it when the question is "how much",
  "what changed", or "what does this touch" rather than "what is wrong with it".
license: Apache-2.0
compatibility: >
  Needs read access to the spec, and to a second version for diffing. Tooling (oasdiff,
  openapi-diff, a spec parser) speeds this up; the analyses here can all be done by reading.
---

# Analysing an API description

This is the measurement counterpart to reviewing a spec. A review asks *what is wrong here*.
Analysis asks *how big, how complex, how risky, what changed, what does it touch* — questions
whose answers are numbers and inventories rather than findings.

They get confused because both involve reading the same file. Keep them apart: a review's
output is a list of things to fix, an analysis's output is a basis for a decision — an
estimate, a go/no-go on a release, a build order.

## Inventory first

Before any judgement, count. Most arguments about a spec are really disagreements about its
size.

| Measure | Why it matters |
|---|---|
| operations, by method | the unit of implementation work |
| operations, by tag | maps to services or teams |
| schemas, and how many are reachable from an operation | unreachable schemas are dead weight |
| maximum and mean `allOf` depth | deep composition generates unusable models |
| polymorphic schemas (`oneOf`/`anyOf`) | the single biggest driver of hand-review effort |
| inline (anonymous) schemas | each one generates an unstable name |
| distinct response status codes | drives error-handling and test count |
| operations with no security requirement | a security question disguised as a count |
| parameters per operation, max | a proxy for interface awkwardness |
| required vs optional field ratio | high optionality means more branches to test |

A spec with 40 operations and 12 polymorphic schemas is a bigger job than one with 120 flat
operations. Counting operations alone will mislead whoever is planning the work.

## Complexity and risk scoring

Weight by what actually costs time, not by line count. Something close to:

| Factor | Weight | Because |
|---|---|---|
| polymorphic request/response schema | high | generated model needs human review before logic is built on it |
| free-form `additionalProperties` | high | unvalidated and untyped; defers the problem to runtime |
| `allOf` beyond ~3 deep | medium | flattening produces a model that no longer resembles the spec |
| per-operation inline schema | medium | unstable generated names |
| operation count over ~50 | medium | coordination cost, not per-operation cost |
| schema count over ~100 | low–medium | review surface |
| many distinct error statuses | low | mostly mechanical |

Report the score **with its drivers**, never alone. "Risk 62/100" is unactionable; "risk 62:
nine polymorphic schemas, two `allOf` chains five deep" tells someone what to look at. A score
with no drivers gets argued with; a score with drivers gets acted on.

## Diffing two versions

This is the highest-value analysis, because a breaking change is invisible in a line diff — the
diff shows an added line, not that a client will now fail.

**Breaking for clients (consumers of the API):**

| Change | Effect |
|---|---|
| operation removed, or `operationId` renamed | generated client method disappears |
| response property removed, or its type narrowed | client deserialization fails |
| new property added to `required` | previously valid requests become invalid |
| enum value removed | existing payloads rejected |
| optional request property made required | same |
| success status code changed | client branches on the old one |
| error response removed | client loses a handled case |
| parameter made required, or its type narrowed | existing calls rejected |
| `format` added to an existing field | tightens validation on data already in flight |

**Breaking for the server (you, implementing it):**

| Change | Effect |
|---|---|
| new required request property | new validation, new 400 case, new tests |
| new enum value | new branch, and a parameterised test to extend |
| schema moved into a `oneOf` | model becomes a hierarchy; mappers change |
| scope added to an operation | security config and 403 tests change |

**Additive and safe:** a new optional response property, a new operation, a new optional
parameter with a default, a loosened constraint, a new error status the client can treat as
generic.

Classify every entry in a diff into one of those three buckets and say which. A diff report
that lists changes without classifying them leaves the reader doing the hard part.

## Blast radius on existing code

When a spec change lands on a service that already exists, the useful output is *which files
will need attention*:

1. **Operations added** → a new delegate method, a service method, and the operation's test
   cases.
2. **Operations removed** → hand-written code is now orphaned. Name it; do not delete it.
   Whoever owns the product decides whether it is going.
3. **Request/response type changed** → the mapper will fail to compile if it is strict, which
   is the cheapest place to find out. List the mappers.
4. **Field renamed** → everything that reads it. If generated types are confined to a boundary
   layer, that is a bounded list; if they have leaked into services and tests, it is not, and
   that is worth reporting as a finding in its own right.
5. **Field made required** → a new 400 path and a test for it.
6. **Enum value added** → every exhaustive `switch` over it, plus the parameterised test.

The honest version of this report includes what you could not determine. "Three call sites I
could not resolve statically" is useful; silently omitting them is not.

## Coverage: is the spec actually implemented and tested?

Three mappings worth producing, because gaps hide in the joins:

- **spec → code**: every `operationId` has an implementation. A missing one is unimplemented
  contract — the API documents something that 501s or 404s.
- **code → spec**: every endpoint appears in the spec. An extra one is undocumented surface,
  which is also unversioned and unsecured-by-review.
- **spec → tests**: every operation, and every declared status code, has a test. An untested
  declared error path is the most common gap, because the happy path gets written first and
  the error paths get written never.

Report these as counts and a list, not a percentage. "94% covered" hides which 6%.

## Explaining a large unfamiliar spec

Reading 8,000 lines top to bottom is the wrong approach. In order:

1. `info`, `servers`, `security` — what this is, where it lives, how it is authenticated.
2. `tags` — the intended grouping, which is the author's own mental model.
3. The path list alone. The resource model is usually obvious from the URLs.
4. The three or four schemas referenced most often. Those are the domain.
5. Only then, individual operations — and only the ones that matter for the question at hand.

Lead with what the API is *for*, in a sentence. An analysis that starts with counts makes the
reader assemble the picture themselves.

## Reporting

State the method and its limits. If the diff was computed by a tool, name it and note what it
does not detect (most do not catch semantic changes to a `description`, or a constraint
tightened via `pattern`). If counts came from reading, say so.

An analysis is used to make a decision. The reader needs to know how much to trust it, and a
confident number with no stated method invites exactly the wrong amount of confidence.
