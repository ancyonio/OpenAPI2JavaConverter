---
applyTo: "**/src/test/java/**/*.java"
---

# Tests

- JUnit 5 + Mockito + AssertJ. Never JUnit's bare `assertEquals`.
- Name: `methodName_stateUnderTest_expectedBehaviour`.
- `@ExtendWith(MockitoExtension.class)` with strict stubs. `@Mock` fields, never
  `Mockito.mock(...)` in a field initializer.
- Never mock DTOs, records, value objects or `Clock`. Inject a fixed `Clock`.
- Never stub the class under test.
- `@WebMvcTest` for API slices with `@MockitoBean` services. `@SpringBootTest` +
  Testcontainers for integration. Never an in-memory database standing in for production.
- Arrange-Act-Assert, visibly separated. One logical assertion per test; group with
  `satisfies(...)` or `SoftAssertions`.
- `@ParameterizedTest` for validation boundaries and enum coverage.
- Deterministic: no `Thread.sleep`, no real network, no system clock, no unseeded random, no
  reliance on execution order.
- Every test must be capable of failing. If no production change would break it, delete it.
- Never add a test purely to move a coverage number.
