# Version traps, with before and after

Every entry here is a construct that parses, validates, and then gets discarded by the
generator. The symptom is always the same shape: generated code that quietly disagrees with
the spec, and no diagnostic anywhere.

## Contents

- [Nullability](#nullability)
- [Exclusive numeric bounds](#exclusive-numeric-bounds)
- [`$ref` with siblings](#ref-with-siblings)
- [Required vs optional](#required-vs-optional)
- [Enums and unknown values](#enums-and-unknown-values)
- [Discriminators and polymorphism](#discriminators-and-polymorphism)
- [Quick version-detection table](#quick-version-detection-table)

## Nullability

OpenAPI 3.0 has a `nullable` keyword. OpenAPI 3.1 adopted JSON Schema 2020-12, which does not
— nullability is a type union. Each form is inert in the other version.

**3.0, correct:**

```yaml
cancelledAt:
  type: string
  format: date-time
  nullable: true
```

**3.1+, correct:**

```yaml
cancelledAt:
  type: ["string", "null"]
  format: date-time
```

**3.1 document with the 3.0 form — the common defect:**

```yaml
openapi: "3.1.2"
# ...
cancelledAt:
  type: string
  format: date-time
  nullable: true        # unknown keyword in 3.1; ignored
```

Generated result: a non-nullable field. Jackson then rejects a `null` the author believed was
allowed, and the failure surfaces as a deserialization error in production against real data
that happens to contain nulls.

This is the defect to look for first when a spec has been upgraded from 3.0 to 3.1, because
bumping the version string is the easy part and nobody sweeps the schemas.

## Exclusive numeric bounds

3.0: a boolean that modifies `minimum`/`maximum`. 3.1: the bound itself, as a number.

**3.0, correct:**

```yaml
quantity:
  type: integer
  minimum: 0
  exclusiveMinimum: true     # means "> 0"
```

**3.1+, correct:**

```yaml
quantity:
  type: integer
  exclusiveMinimum: 0        # means "> 0"
```

**3.1 document with the 3.0 form:**

```yaml
quantity:
  type: integer
  minimum: 0
  exclusiveMinimum: true     # boolean is not valid here; dropped
```

Generated result: either no constraint, or `@Min(0)` instead of `@DecimalMin(value="0",
inclusive=false)`. A quantity of `0` then passes validation. This one is worth checking
carefully on monetary and quantity fields, where the off-by-one is a business rule.

## `$ref` with siblings

In 3.0 every sibling of `$ref` is ignored. In 3.1 siblings are legal, but generators vary in
whether they apply them. Since the siblings are usually the constraints or the documentation,
losing them is invisible.

**Risky:**

```yaml
shippingAddress:
  $ref: '#/components/schemas/Address'
  description: Where to deliver
  readOnly: true
```

**Portable:**

```yaml
shippingAddress:
  allOf:
    - $ref: '#/components/schemas/Address'
  description: Where to deliver
  readOnly: true
```

The `allOf` wrapper is honoured by every version and every generator.

## Required vs optional

`required` is a list on the *parent object*, not a flag on the property. A property with a
default is still optional unless listed. Two things to check:

- A property the author describes as mandatory but that is absent from `required` generates a
  nullable field with no validation.
- Adding a name to `required` is a **breaking change** for existing clients. Flag it as such
  when reviewing a diff, not just as an improvement.

## Enums and unknown values

An enum generates a closed Java/TypeScript enum. Two consequences worth stating in review:

- Deserializing an unknown value fails. If the API may add enum values, the spec should say
  so and clients should be built to tolerate it — otherwise every producer-side addition is a
  breaking change for consumers.
- Adding a value is safe for consumers only if they already tolerate unknowns; removing one is
  always breaking.

Check that every enum has a test covering each value, and one covering an unknown value.

## Discriminators and polymorphism

`oneOf`/`anyOf` with a `discriminator` generates a type hierarchy. Things that go wrong:

- The `propertyName` must exist in every variant and be `required` in each. If it is not,
  deserialization picks a branch arbitrarily or fails.
- An implicit mapping derives subtype names from schema names, so renaming a schema changes
  the wire format. Prefer an explicit `mapping`.
- Nested polymorphism (a `oneOf` inside an `allOf` member) is the shape generators handle
  worst. Flag it for human review of the generated model before any logic is built on it.

## Quick version-detection table

When a document's declared version and its constructs disagree, this tells you which way:

| Construct present | Implies |
|---|---|
| `swagger: "2.0"`, `definitions`, `in: body`, `produces`/`consumes` | Swagger 2.0 |
| `nullable`, boolean `exclusiveMinimum`, `components` | 3.0.x |
| `type` as an array, numeric `exclusiveMinimum`, `$schema`, `webhooks`, `const` | 3.1.x |
| `additionalOperations`, `querystring`, `$self`, tag `parent`/`kind` | 3.2.x |

A document declaring 3.1 but full of the 3.0 column was upgraded by editing the version string
alone. Report it as one finding — "this document declares 3.1 but is written as 3.0" — with the
list attached, rather than as one finding per schema.
