---
applyTo: "**/src/main/java/**/*.java"
---

# Hand-written Spring Boot code

- Constructor injection, `private final` fields. Never field or setter injection.
- Delegates are thin: map, call one service method, map back, return `ResponseEntity`.
- Generated DTOs (`..api.model..`) never appear outside `..api..` and `..mapper..`.
- `@Transactional` on service methods only; `readOnly = true` for queries. Remember that
  `@Transactional` on a private or self-invoked method does nothing — the proxy is bypassed.
- Throw domain exceptions. Never `ResponseStatusException`, never bare `RuntimeException`.
- Errors return RFC 9457 `ProblemDetail` with `application/problem+json`, via one
  `@RestControllerAdvice`. Never leak stack traces, SQL or internal class names into `detail`.
- Inject `Clock` and ID suppliers. Never call `Instant.now()` or `UUID.randomUUID()` inline —
  it makes the method untestable.
- Every outbound call sets connect and read timeouts.
- MapStruct mappers use `unmappedTargetPolicy = ERROR`.
- Use only Java features available at `target.java_version` in `o2j.yaml`. No preview features.
- Javadoc on public service methods: what it does, what it throws, and why — not restated
  parameter names.
