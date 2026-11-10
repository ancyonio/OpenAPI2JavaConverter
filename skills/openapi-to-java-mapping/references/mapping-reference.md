# Complete mapping reference

The main skill covers the choices that matter most. This is the full table plus worked examples
of the two shapes that go wrong most often.

## Contents

- [Full format table](#full-format-table)
- [Generator option equivalents](#generator-option-equivalents)
- [Worked example: a discriminated union](#worked-example-a-discriminated-union)
- [Worked example: a PATCH model](#worked-example-a-patch-model)
- [Collection and map edge cases](#collection-and-map-edge-cases)
- [Things with no clean Java equivalent](#things-with-no-clean-java-equivalent)

## Full format table

| `type` | `format` | Java | Notes |
|---|---|---|---|
| `string` | — | `String` | |
| `string` | `date` | `LocalDate` | |
| `string` | `date-time` | `OffsetDateTime` | RFC 3339 carries an offset; `LocalDateTime` discards it |
| `string` | `time` | `OffsetTime` | `LocalTime` if the API is offset-free by design |
| `string` | `duration` | `Duration` | ISO-8601 duration |
| `string` | `uuid` | `UUID` | |
| `string` | `uri`, `uri-reference` | `URI` | |
| `string` | `email` | `String` + `@Email` | |
| `string` | `hostname` | `String` + `@Pattern` | |
| `string` | `ipv4`, `ipv6` | `String` + `@Pattern` | `InetAddress` resolves DNS — avoid in a DTO |
| `string` | `byte` | `byte[]` | base64-encoded on the wire |
| `string` | `binary` | `Resource` / `InputStream` | streaming; `byte[]` loads the whole body into heap |
| `string` | `password` | `String` | a documentation hint only; never log it |
| `integer` | `int32` | `Integer` | |
| `integer` | `int64` | `Long` | |
| `integer` | — | `Long` | assume the wider type |
| `number` | `float` | `Float` | never for money |
| `number` | `double` | `Double` | never for money |
| `number` | — | `BigDecimal` | |
| `boolean` | — | `Boolean` | |
| `object` | — | a class / record | |
| `array` | — | `List<T>` / `Set<T>` | `Set` when `uniqueItems: true` |
| `null` (3.1, in a union) | — | makes the type nullable | |

Use boxed types for anything not in `required`: a primitive cannot represent absent, so it
becomes `0`/`false` — a value the caller never sent.

## Generator option equivalents

What to set when the generator's default is not what you would choose:

| Concern | openapi-generator option |
|---|---|
| `java.time` rather than `Date` | `dateLibrary=java8` |
| `BigDecimal` for `number` | `bigDecimalAsString=false` + fix the spec's `format` |
| avoid the nullable wrapper | `openApiNullable=false` |
| `Optional` on optional fields | `useOptional` — usually leave `false`; it fits returns, not fields |
| Bean Validation annotations | `useBeanValidation=true`, `performBeanValidation=true` |
| `jakarta` rather than `javax` | `useSpringBoot3=true` (or `useJakartaEe=true`) |
| no generation timestamp | `hideGenerationTimestamp=true` — required for reproducible output |
| `Set` for `uniqueItems` | on by default in recent versions; verify |

If a default is wrong for your project, change it in configuration rather than editing the
output. Editing generated files is lost on the next build.

## Worked example: a discriminated union

**Spec:**

```yaml
components:
  schemas:
    PaymentMethod:
      oneOf:
        - $ref: '#/components/schemas/CardPayment'
        - $ref: '#/components/schemas/BankTransfer'
      discriminator:
        propertyName: kind
        mapping:
          card: '#/components/schemas/CardPayment'
          bank: '#/components/schemas/BankTransfer'

    CardPayment:
      type: object
      required: [kind, last4]
      properties:
        kind: { type: string, enum: [card] }
        last4: { type: string, pattern: '^[0-9]{4}$' }

    BankTransfer:
      type: object
      required: [kind, iban]
      properties:
        kind: { type: string, enum: [bank] }
        iban: { type: string }
```

**Java (sealed interface, 17+):**

```java
@JsonTypeInfo(use = JsonTypeInfo.Id.NAME, property = "kind", visible = true)
@JsonSubTypes({
  @JsonSubTypes.Type(value = CardPayment.class, name = "card"),
  @JsonSubTypes.Type(value = BankTransfer.class, name = "bank")
})
public sealed interface PaymentMethod permits CardPayment, BankTransfer {}

public record CardPayment(
    @NotBlank @Pattern(regexp = "^[0-9]{4}$") String last4) implements PaymentMethod {}

public record BankTransfer(@NotBlank String iban) implements PaymentMethod {}
```

Three things this gets right:

- **`mapping` is explicit.** An implicit mapping derives wire values from schema names, so
  renaming `CardPayment` silently changes the wire format.
- **`kind` is `required` in every variant.** Without that, the deserializer cannot dispatch.
- **`sealed`** makes a `switch` over the variants exhaustive, so adding a third one becomes a
  compile error at every site that handles them rather than a runtime surprise.

`visible = true` is needed only if the subtype itself must read `kind`; otherwise Jackson
consumes it.

## Worked example: a PATCH model

The one case where absent and explicit-null genuinely differ: absent means *leave alone*, null
means *clear*.

```yaml
UpdateCustomerRequest:
  type: object
  properties:
    email:       { type: string }
    phoneNumber: { type: ["string", "null"] }    # nullable: may be cleared
```

```java
public record UpdateCustomerRequest(
    // absent -> do not change; present -> set
    @Email String email,
    // absent -> do not change; present-and-null -> clear; present -> set
    JsonNullable<String> phoneNumber) {}
```

Applying it:

```java
if (request.email() != null) {
  customer.setEmail(request.email());
}
if (request.phoneNumber().isPresent()) {
  customer.setPhoneNumber(request.phoneNumber().get());   // may legitimately be null
}
```

This is genuinely awkward, so adopt it only where clearing a field is a real requirement. For
most APIs one null meaning is enough and `openApiNullable=false` keeps the model plain. If the
three-state semantics are needed, say so in the spec's `description` — otherwise the next person
simplifies it away.

## Collection and map edge cases

| Spec | Java | Note |
|---|---|---|
| `array` of `$ref` | `List<T>` | default to empty, never null |
| `array`, `uniqueItems: true` | `Set<T>` | needs `equals`/`hashCode` on the element |
| `array` of `array` | `List<List<T>>` | usually a modelling smell |
| `additionalProperties: true` | `Map<String, Object>` | unvalidated; last resort |
| `additionalProperties: {type: string}` | `Map<String, String>` | |
| `properties` + `additionalProperties` | class + `@JsonAnySetter` overflow map | |
| `additionalProperties: false` | class, plus `fail-on-unknown-properties` | makes the closed-ness real |

An empty-default collection is worth insisting on: a null list forces every caller into a null
check, and the one that forgets throws in production.

## Things with no clean Java equivalent

Flag these as spec issues rather than contorting the Java:

| Spec | Problem |
|---|---|
| `oneOf` with no `discriminator` | the deserializer guesses by trying branches, and guesses wrong on overlapping shapes |
| `anyOf` | rarely the author's intent; usually they meant `oneOf` |
| `prefixItems` (3.1 tuples) | heterogeneous fixed-length arrays have no idiomatic Java form |
| `if`/`then`/`else`, `dependentSchemas` | no generator implements these, so the constraint exists in the spec and is not enforced in code |
| `patternProperties` | no mapping; becomes an unvalidated map |
| deeply nested `allOf` | flattens into one class that no longer resembles the spec |
| `not` | no mapping at all |

For the conditional keywords specifically: if you keep them, the validation must be written by
hand in the service layer. Otherwise the spec promises a rule the service does not enforce,
which is worse than not declaring it.
