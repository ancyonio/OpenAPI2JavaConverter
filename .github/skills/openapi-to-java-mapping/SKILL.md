---
name: openapi-to-java-mapping
description: >
  Decide what Java type an OpenAPI schema should become — formats to temporal and numeric
  types, nullability, enums, oneOf and allOf to Java's single-inheritance model, maps, arrays,
  binary and money — and predict what a generator will actually emit. Use this whenever you
  are choosing or reviewing the Java representation of a schema, whenever a generated DTO has
  a surprising type (a date as String, a number as Double, an enum as String), whenever you
  are hand-writing a DTO or domain type to match a contract, and whenever you need to know
  how a spec construct will survive the trip into Java. Reach for it when a field's type is
  the question, because the wrong type choice is invisible in review and shows up as a
  rounding error, a timezone bug or a failed deserialization in production.
license: Apache-2.0
compatibility: >
  Java 17+. Mapping tables assume Jackson for serialisation and Jakarta Bean Validation for
  constraints; notes where openapi-generator's defaults differ from what you would choose.
---

# Mapping OpenAPI schemas to Java types

A generator will produce *a* type for every schema. Whether it produces the *right* type is a
separate question, and the wrong answer is nearly invisible: the code compiles, the tests pass
with round-number fixtures, and the defect surfaces as a rounding error on a real invoice or a
timezone shift on a real timestamp.

Three choices account for most of the damage: **money as a floating-point type, timestamps as
a type with no offset, and a nullable field generated as non-nullable.** If you check nothing
else, check those.

## Primitives and formats

| `type` / `format` | Use | Not |
|---|---|---|
| `string` | `String` | |
| `string` / `date` | `LocalDate` | `Date`, `String` |
| `string` / `date-time` | `OffsetDateTime` | `LocalDateTime` — it has no offset, so it silently reinterprets the instant |
| `string` / `time` | `OffsetTime` or `LocalTime` | |
| `string` / `duration` | `Duration` | |
| `string` / `uuid` | `UUID` | `String` |
| `string` / `uri` | `URI` | |
| `string` / `byte` | `byte[]` (base64) | |
| `string` / `binary` | `Resource` / `InputStream` — stream it | `byte[]`, which loads the whole upload into heap |
| `string` / `email`, `hostname`, `ipv4` | `String` + a validation annotation | a custom type nobody else uses |
| `integer` / `int32` | `Integer` | |
| `integer` / `int64` | `Long` | `Integer` — the overflow is silent |
| `integer` (no format) | `Long` — assume the wider type | |
| `number` / `float`, `double` | `Float`, `Double` | anything monetary |
| `number` (no format) | `BigDecimal` | |
| `boolean` | `Boolean` | |

**Money and quantities that must be exact: `BigDecimal`, always.** `0.1 + 0.2 != 0.3` in
binary floating point, so a `Double` total is wrong by amounts that pass every test with
round-number fixtures and fail reconciliation. If a spec declares a monetary field as
`number/double`, that is a defect in the spec worth raising — and map it to `BigDecimal`
regardless.

Use the boxed types (`Integer`, not `int`) for any field that is not `required`. A primitive
cannot express absent, so it silently becomes `0` or `false`, which is a value the caller never
sent.

## Nullability, and the three states

A JSON field has three states and Java has two, which is the root of most nullability
confusion:

| JSON | Java | How |
|---|---|---|
| absent | field not set | `Boolean`/`Integer` etc. left null |
| `null` | explicit null | the type must be nullable |
| a value | the value | |

If the API genuinely needs to distinguish "absent" from "explicitly null" — for a PATCH, where
absent means *leave alone* and null means *clear* — Java needs a wrapper (`JsonNullable`, or an
`Optional<Optional<T>>`-shaped idiom). That complicates every mapper and every test, so adopt
it only where the distinction is real. For most APIs, one `null` meaning is enough, and a
generator option like `openApiNullable=false` keeps the model plain.

Remember that the spec's own nullability form differs by version: `nullable: true` in 3.0, a
`["string","null"]` type union in 3.1. The wrong form for the declared version is silently
ignored and the field generates non-nullable.

## Required and validation

`required` is a list on the parent object, not a property flag. A property with a `default` is
still optional unless listed.

| Spec | Annotation |
|---|---|
| in `required` | `@NotNull` (`@NotBlank` for a string that must have content) |
| `minLength` / `maxLength` | `@Size(min=, max=)` |
| `pattern` | `@Pattern(regexp=)` |
| `minimum` / `maximum` | `@Min` / `@Max`, or `@DecimalMin` / `@DecimalMax` |
| `exclusiveMinimum: N` (3.1) | `@DecimalMin(value="N", inclusive=false)` |
| `minItems` / `maxItems` | `@Size` on the collection |
| `uniqueItems: true` | a `Set`, or a custom validator — Bean Validation has no built-in |
| nested object | `@Valid`, or nested constraints are not checked at all |

That last row is the one most often missed: without `@Valid` on a nested field, the nested
object's own constraints never run, so an invalid child passes validation silently.

## Composition: `allOf`, `oneOf`, `anyOf`

Java has single inheritance; JSON Schema composition does not map onto it cleanly. What
actually happens:

- **`allOf`** — flattened into one class containing every member's fields. Shallow composition
  flattens sensibly; past about three levels the result no longer resembles the spec and a
  reviewer can no longer tell whether it is right.
- **`oneOf` / `anyOf` with a `discriminator`** — a sealed interface (Java 17+) or an abstract
  base with Jackson subtype annotations. This works, and it is the form to prefer.
- **`oneOf` without a discriminator** — the deserializer must guess by trying each branch. It
  will guess wrong on overlapping shapes. Treat a missing discriminator as a spec defect.
- **`anyOf`** — rarely what the author meant. Usually they wanted `oneOf`.

For a discriminated union, the `propertyName` must exist and be `required` in **every** variant,
and prefer an explicit `mapping` — an implicit one derives wire values from schema names, so
renaming a schema changes the wire format.

## Objects, maps and arrays

| Spec | Java |
|---|---|
| `type: object` with `properties` | a class (a record if immutable and there is no framework need for setters) |
| `additionalProperties: true` | `Map<String, Object>` — unvalidated, so avoid where possible |
| `additionalProperties: {$ref}` | `Map<String, T>` |
| `properties` + `additionalProperties` | a class plus an overflow map (`@JsonAnySetter`) |
| `type: array` | `List<T>` |
| `type: array`, `uniqueItems: true` | `Set<T>` |
| `prefixItems` (3.1 tuple) | no clean Java equivalent — usually a defect in the spec |

Default collections to empty, never null. A null list forces every caller into a null check,
and the one that forgets throws in production.

## Enums

A spec enum becomes a Java enum, which is closed: an unknown value fails deserialization.

That has a consequence worth stating to whoever owns the spec — **adding an enum value is a
breaking change for any consumer that cannot tolerate unknowns.** If the API expects to grow
values, either the clients must handle unknowns (a catch-all constant, or a string plus
validation) or every addition needs a version bump.

Check too that enum values are valid Java identifiers. `in-progress` and `2xx` are not; a
generator will transliterate them in a way that varies between versions, so the Java constant
name is not stable across generator upgrades.

## Names

Generated names come from the spec, so spec hygiene is Java hygiene: camelCase properties,
PascalCase schema names, and no anonymous inline objects — those become ordinal names that
renumber when the document is edited.

Watch for Java reserved words (`class`, `package`, `new`) and for two schemas whose names
differ only in case, which collide on a case-insensitive filesystem.

## Reference files

- `references/mapping-reference.md` — the complete table including the rare formats, generator
  option equivalents, and worked examples of a discriminated union and a PATCH model.
