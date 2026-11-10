---
name: openapi-version-migration
description: >
  Convert an API description between OpenAPI versions — Swagger 2.0 up to 3.0, 3.0 to 3.1, and
  3.2 down to 3.1 where tooling cannot read 3.2 — and judge honestly what each conversion
  loses. Use this whenever you are asked to upgrade, downgrade, convert or modernise a Swagger
  or OpenAPI document, whenever a code generator or parser rejects a spec as an unsupported
  version, whenever someone asks "can we move to OpenAPI 3.1 / 3.2?", and whenever a document
  declares one version but is written in the style of another. Reach for it before hand-editing
  a version string, because bumping the string without converting the constructs is the single
  most common way a spec starts generating wrong code.
license: Apache-2.0
compatibility: >
  Needs file read/write access. Converter CLIs (swagger2openapi, o2j normalize) help but are
  not required; the mappings here are complete enough to convert by hand.
---

# Migrating an OpenAPI document between versions

A version migration is not a reformat. Each version boundary changes what can be *expressed*,
so every conversion either gains room or loses it, and the losses are what matter.

Three rules carry most of the value:

1. **Never bump the version string alone.** A document declaring `3.1.2` while using
   `nullable: true` and boolean `exclusiveMinimum` is worse than one honestly declaring `3.0.4`,
   because tooling now silently discards those keywords instead of honouring them.
2. **Classify every loss before you accept it.** Some constructs degrade harmlessly; others
   change what the API does. Those are different outcomes and should not be reported alike.
3. **Make the output deterministic.** If the converted document is the input to code
   generation, converting twice must produce identical bytes, or the generated code has no
   stable identity. See `openapi-codegen-determinism`.

## Deciding the target version

Target the newest version your *whole* toolchain can read, not the newest that exists. Check
the parser rather than the spec, because the parser is the constraint.

As of the time of writing, **much of the Java and Python ecosystem cannot read OpenAPI 3.2 at
all** — `swagger-parser`, which `openapi-generator` and a good deal of other tooling parse
through, has an open request for 3.2 support
([`swagger-api/swagger-parser#2248`](https://github.com/swagger-api/swagger-parser/issues/2248)).
The Swagger OSS announcement of 3.2 support covered Swagger UI, Client, Editor and ApiDOM, not
the parser. **Re-check that issue before assuming a 3.2 downgrade is still necessary** — if
parser support has shipped, the downgrade should be retired rather than quietly maintained.

So in practice: 3.1.x is usually the right target. 3.0.4 is right when a consumer is pinned to
3.0 tooling. 3.2 is right only once you have verified every parser in the chain.

## Swagger 2.0 → OpenAPI 3.0

The constructs that genuinely change shape, rather than merely moving:

| 2.0 | 3.0 |
|---|---|
| `definitions` | `components.schemas` (rewrite every `$ref`) |
| `securityDefinitions` | `components.securitySchemes` |
| top-level `responses` | `components.responses` |
| `host` + `basePath` + `schemes` | `servers[].url` |
| `parameters` with `in: body` | `requestBody` |
| `parameters` with `in: formData` | `requestBody` with a form media type |
| parameter-level `type`/`format` | parameter-level `schema` |
| `produces` / `consumes` | per-response / per-request-body `content` keys |
| `response.schema` | `response.content.<media>.schema` |
| `x-nullable` | `nullable: true` |
| `collectionFormat` | `style` + `explode` |

Two judgement calls this conversion forces on you:

**Media types for error responses.** 2.0's `produces` is operation-wide, so it cannot say that
`200` returns `application/json` while `400` returns `application/problem+json`. A mechanical
conversion gives every response the same media type, which then fails any house rule requiring
a problem+json error contract — and the spec author cannot fix it in 2.0, because 2.0 cannot
express it. Conversion is therefore the only place that distinction can be introduced. Decide
deliberately, and record the decision; do not emit a finding the author is powerless to act on.

**Form data.** A 2.0 operation with `formData` parameters becomes one request body with an
object schema. Give that schema a real name in `components.schemas` rather than leaving it
inline, or you inherit the ordinal-naming problem described in `openapi-contract-review`.

## OpenAPI 3.0 → 3.1

3.1 aligned with JSON Schema 2020-12. This is the boundary where a careless migration does the
most damage, because almost everything still parses.

| Change | What to do |
|---|---|
| `nullable: true` is gone | Rewrite as `type: ["string", "null"]` |
| `exclusiveMinimum`/`exclusiveMaximum` become numeric | Move the bound in, drop the paired `minimum`/`maximum` |
| `$ref` may have siblings | Legal now, but generator support varies — keep the `allOf` wrapper |
| `example` → `examples` (an array) | Update, or the example is dropped from docs |
| `webhooks` added | Optional; nothing to migrate |
| `const` added | Can replace single-value enums |
| File upload via `type: string, format: binary` | Now `contentMediaType` / `contentEncoding` |

**Mechanically rewrite every schema when you bump 3.0 → 3.1.** This is not optional cleanup.
A 3.1 document retaining 3.0 nullability generates non-nullable fields, and nothing warns you.

## OpenAPI 3.1 → 3.2 (and back)

3.2 added expressiveness that 3.1 has no equivalent for, which makes the downgrade lossy in a
way the other conversions are not. Classify every affected construct:

**Degradable** — report and continue:

| Construct | Degrades to |
|---|---|
| Nested tag hierarchy (`parent`, `kind`) | A flat standalone tag. Only documentation nesting is lost; interface grouping is unaffected. |
| `$self` | Folded into `servers`, or dropped. Advisory only; no effect on generated code. |

**Load-bearing** — stop and escalate:

| Construct | Why it cannot degrade |
|---|---|
| `additionalOperations` | Declares non-standard HTTP methods (`QUERY`, say). 3.1 has no way to express them, so the endpoints disappear entirely and clients calling them would get 405. |
| `querystring` (whole-querystring schema) | No 3.1 representation, so query validation is lost silently. |

A load-bearing loss should **fail the conversion**, not warn. An API that silently lost an
endpoint is worse than a conversion that refused to run, because the first failure surfaces at
a caller and the second surfaces at your desk.

If a load-bearing construct blocks you, the options are: express the operation with a standard
HTTP method, keep the spec at 3.2 and use tooling that reads it, or wait for parser support.
Say which you recommend rather than dropping the construct quietly.

## Order of operations

Converting in the wrong order produces findings about constructs a later step was about to
create correctly.

1. Convert the version (2.0 → 3.0, or 3.2 → 3.1) **first**.
2. Canonicalise nullability and numeric bounds to the target version's single form.
3. Bundle external `$ref` into one self-contained document.
4. Hoist inline schemas into `components.schemas` with names derived from the operation and
   context — never ordinals, which renumber when the document is edited.
5. Flatten `allOf` beyond your depth budget.
6. Serialise deterministically: stable key order, fixed indentation, no anchors, LF endings.
7. Hash the output. That hash is the identity of anything generated from it.
8. **Now** validate. Validate the *converted* document, because that is what code will be
   generated from, and therefore that is what has to meet the standard.

That last point is worth stating plainly: a 2.0 document fails 3.x-shaped house rules by
construction — it has no `content` blocks, no per-response media types, no parameter schemas.
Validating before conversion produces a page of findings that conversion was about to fix.

## Reporting a migration

Give: source version → target version, the conversions applied, degradable losses with their
JSON Pointers, load-bearing losses with their impact in terms of API behaviour, anything
redacted, and the output hash.

If the conversion was lossless, say so plainly — a migration report that buries "no behaviour
changed" under a list of mechanical rewrites gets skimmed, and then the one time it *does*
matter nobody is reading.

## Reference files

- `references/mapping-tables.md` — the complete field-by-field mappings for each boundary,
  including the ones omitted above as rarely used.
