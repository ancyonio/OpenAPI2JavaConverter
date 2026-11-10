---
name: spring-boot-rest-service
description: >
  Build or review a Spring Boot REST service to production standard — controller and delegate
  design, layering, exception handling with RFC 9457 error responses, dependency injection,
  transaction boundaries and testability. Use this whenever you write, extend or review Spring
  Boot controllers, delegates, services, mappers, exception handlers or @RestControllerAdvice;
  whenever you design a REST controller or decide what a delegate should do; whenever you
  implement an endpoint against a generated API interface; and whenever you are asked to review
  Java or Spring code for correctness, idiom or production-readiness. Reach for
  it even on a small change, because the mistakes it covers — a silently non-transactional
  method, an inline clock call, a missing read timeout — all compile, pass review, and fail in
  production.
license: Apache-2.0
compatibility: >
  Spring Boot 3.x or 4.x on Java 17+. Assumes Maven or Gradle, Spring Web MVC (notes where
  WebFlux differs), and is written for the delegate pattern used by generated API interfaces.
---

# Building a Spring Boot REST service

Most of what goes wrong in a Spring service is not a bug anyone writes on purpose. It is a
construct that *looks* right, compiles, passes review, and does nothing — or does something
subtly different from what it says. This skill is organised around those.

## Layering

```
Generated API interface (controller)   <- generated, never edited
        | implements
Delegate impl  (api package)           <- thin: map, call one service method, map back
        | maps DTO -> domain via a mapper, then calls
Service  (service package)             <- all business logic; the @Transactional boundary
        |
Repository / HTTP client               <- Spring Data, declarative clients
```

**The rule that carries the most weight: generated DTOs must not leave the `api` and `mapper`
packages.** A service method that takes a generated DTO looks harmless and costs you the
ability to regenerate: rename a field in the spec and the change ripples through every service
and every test. Confine DTOs to the boundary and the blast radius of a spec change is two
packages. This is worth enforcing with an ArchUnit rule rather than a convention, because it
is exactly the kind of rule that erodes one pragmatic exception at a time.

The mapper is the price of that isolation. Configure it strictly —
`unmappedTargetPolicy = ERROR` in MapStruct — so that a spec change which would silently drop
a field becomes a compile error instead. A compile error is the cheapest possible place to
find that out.

## Delegates

Keep them boring. Map the request, call exactly one service method, map the result, return
`ResponseEntity` with the status the contract declares.

- `201 Created` sets a `Location` header.
- `204 No Content` for a delete with no body.
- No `try`/`catch` for business failures — throw a domain exception and let the advice map it.
- No validation logic: Bean Validation on the generated DTOs has already run.
- No business decisions. If you find yourself writing an `if` about domain state here, it
  belongs in the service.

## Services

```java
@Service
public class OrderService {

  private final OrderRepository orders;
  private final PaymentClient payments;
  private final Clock clock;

  public OrderService(OrderRepository orders, PaymentClient payments, Clock clock) {
    this.orders = orders;
    this.payments = payments;
    this.clock = clock;
  }

  @Transactional
  public Order create(CreateOrderCommand command) { ... }

  @Transactional(readOnly = true)
  public Page<Order> list(Pageable pageable) { ... }
}
```

**Constructor injection, `private final` fields.** Not a style preference: a field-injected
class cannot be constructed in a test without a container or reflection, so field injection
quietly makes a class untestable and the test suite then works around it.

**`@Transactional` on the service, and only there.** Two traps:

- On a **private, package-private or self-invoked** method it does nothing. Spring proxies the
  bean, and a self-call bypasses the proxy, so the annotation is inert and the code looks
  correct. This is the most common silent correctness bug in Spring services.
- On a controller it holds a transaction open across serialisation; on a repository it is
  redundant and hides where the boundary actually is.

Use `readOnly = true` on queries — it lets the driver and Hibernate skip dirty checking, and
it documents intent.

**Inject `Clock` and an ID supplier.** `Instant.now()` and `UUID.randomUUID()` inline make a
method impossible to assert on, which pushes the test toward asserting nothing. Untestable
code is a design defect, and the service layer is where it gets created or avoided.

**Throw domain exceptions**, never `ResponseStatusException` and never a bare
`RuntimeException`. The service should not know about HTTP; that is the advice's job.

## Error responses

One `@RestControllerAdvice` for the whole service, returning RFC 9457 `ProblemDetail` with
`application/problem+json`. One error shape means one server handler, one client handler and
one set of tests; per-endpoint error shapes multiply all three and drift apart.

Each response carries `type`, `title`, `status`, `detail`, `instance`, and a correlation id.

**Never put an unexpected exception's message into `detail`.** That is the path by which stack
traces, SQL fragments, internal class names and upstream URLs reach callers. Log the cause,
return a generic sentence, and give the caller the correlation id to quote. The same applies to
`HttpMessageNotReadableException`, whose message can echo the request body back.

`references/error-handling.md` has a complete advice implementation.

## Security

- Validate at the boundary with Bean Validation, and make the constraints match the contract.
- `@PreAuthorize` matching the scopes the contract declares — not broader.
- Configure CSRF and CORS deliberately. Defaults are a decision too, just an unexamined one.
- No secrets in source or in `application.yml`. Externalise them.
- Never log credentials, tokens or personal data. A correlation id is the identifier to log.

## Persistence

- No `FetchType.EAGER` on a collection. It turns one query into many, invisibly.
- Watch for N+1: a lazy collection touched in a loop needs a join fetch or an entity graph.
- Every schema change goes through a migration (Flyway/Liquibase). `ddl-auto` above `validate`
  outside tests means production schema drifts from the migrations that are supposed to define
  it.
- Paginate every collection query. An unpaginated finder works against a dev dataset and falls
  over on real data.

## Resilience

- **Every outbound call sets a connect and a read timeout.** A missing read timeout is a
  production incident waiting for a slow dependency — threads pile up on a call that never
  returns.
- Retry only idempotent operations. Retrying a non-idempotent `POST` duplicates side effects.
- Circuit-break downstream dependencies so one slow service does not exhaust your threads.
- Honour `Idempotency-Key` where the contract declares it: a repeated key returns the same
  response and causes exactly one side effect.

## Observability

- Structured (JSON) logging, with a correlation id in the MDC and propagated downstream.
- Meaningful metrics on the service layer, not just HTTP counters.
- Health probes separated into liveness and readiness.

## Java version awareness

Use only what the configured target supports, and no preview features. Records and sealed
types from 17; pattern matching for `switch`, sequenced collections and virtual threads from
21; scoped values and compact source files from 25.

Virtual threads (`spring.threads.virtual.enabled`) are close to free on a 21+ MVC service, but
they make thread-local-heavy code and `synchronized` pinning matter. Adopt them as their own
change, not bundled with something else.

## Enforcing this with ArchUnit

Several rules above are mechanically checkable, and the checkable ones should be checked —
they are precisely the rules that erode under deadline pressure. The set worth having:
layering; generated DTOs confined to `api`/`mapper`; no field injection; `@Transactional` only
on public service methods; no inline `Instant.now()`; no `ResponseStatusException`.

`references/archunit-rules.md` has them as working code.

## Reference files

- `references/error-handling.md` — a complete RFC 9457 `@RestControllerAdvice`, plus the
  domain exception hierarchy it maps.
- `references/archunit-rules.md` — the enforcement rules as a runnable test class, each with
  the reason it exists.
