---
name: spring-validation-error-handling
description: >
  Validate requests at the boundary of a Spring Boot service and turn violations into useful
  400 responses — Jakarta Bean Validation annotations, @Valid versus @Validated, nested and
  cross-field constraints, validation groups, custom validators, and shaping the field-level
  error body a client can act on. Use this whenever a constraint is not firing, whenever
  invalid input reaches a service method, whenever a 400 response tells the caller nothing
  useful, whenever you are adding or reviewing validation annotations on a DTO, and whenever a
  contract's declared constraints need turning into enforced ones. Reach for it when input
  handling is the question, because a constraint that silently does not run looks exactly like
  one that passes.
license: Apache-2.0
compatibility: >
  Spring Boot 3.x/4.x with spring-boot-starter-validation (Jakarta Bean Validation), Java 17+.
  The error-body shape assumes RFC 9457 ProblemDetail.
---

# Validating at the boundary

Validation has one purpose: nothing invalid gets past the edge of the service, so every layer
behind it can assume its inputs are sound. When that holds, service code is about business
rules. When it does not, every method starts with defensive checks and the real logic gets
harder to see.

The failure that matters here is specific and quiet: **a constraint that does not run looks
identical to one that passes.** No error, no log line, no failing test unless someone wrote a
test for the invalid case. Most of this skill is about the ways that happens.

## The four ways a constraint silently does nothing

**1. No `@Valid` on the parameter.**

```java
// annotations on CreateOrderRequest are never evaluated
public ResponseEntity<Order> create(@RequestBody CreateOrderRequest request)

// correct
public ResponseEntity<Order> create(@Valid @RequestBody CreateOrderRequest request)
```

**2. No `@Valid` on a nested field.** Constraints do not cascade by themselves:

```java
public record CreateOrderRequest(
    @NotBlank String customerId,
    @NotEmpty @Valid List<OrderItem> items) {}   // without @Valid, OrderItem's own
                                                 // constraints never run
```

This is the most common version of the bug, because the outer object validates fine and the
invalid child passes through.

**3. `@Valid` on a method parameter without `@Validated` on the class.** For constraints on
parameters of a `@Service` method — as opposed to a controller — the class needs `@Validated`
to get the validating proxy. Without it the annotations are decoration.

**4. Self-invocation.** Same proxy problem as `@Transactional`: a validated method called from
another method of the same bean bypasses the proxy entirely.

When a constraint appears not to work, check these four before suspecting the framework.

## Contract constraints to annotations

If the service has an API description, it already declares the constraints; the job is to not
lose them in translation.

| Spec | Annotation | Note |
|---|---|---|
| in `required` | `@NotNull` | `@NotBlank` if a string must have content |
| `minLength`/`maxLength` | `@Size(min=, max=)` | |
| `pattern` | `@Pattern(regexp=)` | anchor it, or it matches a substring |
| `minimum`/`maximum` | `@Min`/`@Max`, `@DecimalMin`/`@DecimalMax` | `@DecimalMin` for `BigDecimal` |
| `exclusiveMinimum: N` | `@DecimalMin(value="N", inclusive=false)` | |
| `minItems`/`maxItems` | `@Size` on the collection | |
| `format: email` | `@Email` | permissive by design; add `@Pattern` if you need stricter |
| nested object | `@Valid` | see above |

`@NotNull` versus `@NotEmpty` versus `@NotBlank` is worth getting right: `@NotNull` allows
`""`, `@NotEmpty` allows `"   "`, `@NotBlank` allows neither. For a customer id, `@NotBlank`.

## Cross-field rules

Bean Validation is per-field by default; a rule involving two fields needs a class-level
constraint:

```java
@ValidDateRange                              // class-level custom constraint
public record DateRange(@NotNull LocalDate from, @NotNull LocalDate to) {}
```

```java
public class ValidDateRangeValidator
    implements ConstraintValidator<ValidDateRange, DateRange> {

  @Override
  public boolean isValid(DateRange value, ConstraintValidatorContext context) {
    // Null fields are @NotNull's job. Returning true here keeps one violation per problem
    // instead of reporting "range invalid" when the real issue is a missing date.
    if (value.from() == null || value.to() == null) {
      return true;
    }
    if (!value.to().isAfter(value.from())) {
      context.disableDefaultConstraintViolation();
      context.buildConstraintViolationWithTemplate("`to` must be after `from`")
          .addPropertyNode("to")               // attach to a field, so the client can
          .addConstraintViolation();           // point at the right input
      return false;
    }
    return true;
  }
}
```

Two habits in there worth keeping: let `@NotNull` own null so errors do not double up, and
attach a class-level violation to a specific property node — otherwise the client gets an
object-level error with no field to highlight.

## Validation groups

Use these when the same type is valid in different ways in different operations — an `id` that
must be absent on create and present on update:

```java
public record CustomerDto(
    @Null(groups = OnCreate.class) @NotNull(groups = OnUpdate.class) String id,
    @NotBlank String email) {}

public ResponseEntity<Customer> create(
    @Validated(OnCreate.class) @RequestBody CustomerDto dto) { ... }
```

Note `@Validated(Group.class)`, not `@Valid`, which cannot express a group. And be sparing:
groups are easy to write and easy to forget, and a missing group silently validates against
`Default` — which may enforce nothing.

Often the clearer answer is two types, `CreateCustomerRequest` and `UpdateCustomerRequest`.
More classes, but the constraint is obvious from the type.

## The 400 body

A 400 saying "Bad Request" is almost useless — the client knows the request failed and nothing
about which field. Give them the fields:

```json
{
  "type": "https://problems.example.com/orders/validation-failed",
  "title": "Validation failed",
  "status": 400,
  "detail": "The request contains 2 invalid fields.",
  "instance": "/orders",
  "correlationId": "7f3c...",
  "errors": [
    { "field": "items[0].quantity", "message": "must be greater than 0" },
    { "field": "customerId", "message": "must not be blank" }
  ]
}
```

Properties that make this usable: **every** violation, not just the first; a `field` path that
matches the request body's structure including array indices; and a message safe to show a
user.

Do not echo the rejected value back. It is attacker-controlled and ends up in logs, bug
trackers and sometimes rendered in a UI.

Handle `MethodArgumentNotValidException` (body) and `ConstraintViolationException` (parameters
and service-level) — they are different exceptions from the same concept, and handling only the
first leaves parameter violations as a generic 500.

For a malformed body, `HttpMessageNotReadableException`, return a generic message: the parser's
own message can quote the request body back.

## Where validation stops and business rules start

Bean Validation is for *shape*: present, in range, matching a pattern. It is not for rules
needing state or other systems.

| Boundary validation | Service-layer rule |
|---|---|
| `quantity` must be > 0 | the SKU must be in stock |
| `email` must look like an email | the email must not already be registered |
| `currency` must be a 3-letter code | the currency must be one we trade in |

Business rules throw domain exceptions, which map to 409 or 422 rather than 400 — the request
was well-formed, so 400 misdescribes it.

The practical test: if answering the question needs a database or another service, it is not
boundary validation.

## Testing it

For each constrained field, a parameterised test of the boundary values — and crucially, assert
`verifyNoInteractions(service)` on the failure path. That is what proves the boundary rejected
the request rather than the service doing work first and failing later.

Then one test per response shape: the field path is right, all violations are present, and the
rejected value is **not** in the body.
