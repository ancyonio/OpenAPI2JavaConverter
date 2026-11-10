---
name: rest-api-testing
description: >
  Test a REST API at its own boundary — conformance against the API description, end-to-end
  requests through the real stack, stubbing downstream services with WireMock, real databases
  with Testcontainers, consumer-driven contract tests, and managing test data between runs. Use
  this whenever you are writing or reviewing integration, end-to-end, API or contract tests;
  whenever responses drift from what the spec declares; whenever tests need a dependency that
  is slow, flaky or not yours; whenever an in-memory database is standing in for the real one;
  and whenever an API test suite is slow or intermittently failing. Reach for it when the
  question is about testing the API as a running thing, rather than about assertions inside a
  unit test.
license: Apache-2.0
compatibility: >
  Spring Boot 3.x/4.x test support (@SpringBootTest, MockMvc, WebTestClient), Testcontainers,
  WireMock, Java 17+. The conformance section assumes an OpenAPI description exists.
---

# Testing an API at its boundary

Unit tests answer "does this class behave?". API tests answer a different question: **"does the
running service honour its contract?"** A suite can be perfect at the first and silent on the
second — every class correct, and the assembled service returning a field name the contract
never promised.

The specific gaps API tests close, which unit tests structurally cannot:

- serialisation: what actually goes on the wire, including nulls, dates and number formats
- the real HTTP layer: status codes, headers, content negotiation, error shapes
- wiring: whether the beans compose, whether security is applied
- the real database: dialect, constraints, transaction behaviour
- the real contract: that responses validate against the declared schemas

## Choosing the layer

Each costs more and catches more than the one above. Push a test up when it can go up; a slow
suite stops being run.

| Layer | Spins up | Use for |
|---|---|---|
| `@WebMvcTest` slice | the web layer, service mocked | status, headers, JSON shape, validation 400s |
| `@SpringBootTest` + mocks | the whole context, externals mocked | wiring, security, configuration |
| `@SpringBootTest` + Testcontainers | context + real database | persistence, transactions, constraints |
| Contract conformance | context + the spec | that responses match declared schemas |
| End-to-end | deployed services | a few critical journeys only |

Most teams get this upside down: a handful of slice tests and a hundred slow end-to-end ones.
Invert it. End-to-end tests are for the two or three journeys whose breakage is a business
incident; everything else is cheaper and more precise one layer down.

## Conformance against the description

This is the test that makes the API description authoritative rather than decorative. Without
it, the spec and the implementation drift and a consumer finds out.

```java
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
class OpenApiConformanceTest {

  @Autowired private TestRestTemplate restTemplate;
  private static OpenApiInteractionValidator validator;

  @BeforeAll
  static void loadSpec() {
    validator = OpenApiInteractionValidator
        .createForSpecificationUrl("openapi/spec.yaml")
        .build();
  }

  @Test
  void createOrder_responseMatchesDeclaredSchema() {
    var response = restTemplate.postForEntity("/orders", validRequest(), String.class);

    // Validates status, headers, media type and body against the operation's declaration.
    assertThat(validator.validateResponse("/orders", Request.Method.POST, asResponse(response)))
        .satisfies(report -> assertThat(report.hasErrors())
            .as("%s", report)
            .isFalse());
  }
}
```

Validate the **error** responses too, not just the happy path. The error schemas are the ones
nobody checks, so they are the ones that drift.

Two traps: a conformance test that only exercises operations someone remembered to add proves
nothing about the rest, so drive it from the spec's own operation list; and validating only the
response misses a request your own client is building wrongly.

## Stubbing what is not yours

A downstream service in a test is someone else's availability, latency and test data. WireMock
replaces it with something you control:

```java
@SpringBootTest(webEnvironment = RANDOM_PORT)
@AutoConfigureWireMock(port = 0)
class PaymentIntegrationTest {

  @Test
  void createOrder_whenPaymentGatewayTimesOut_returns503AndPersistsNothing() {
    stubFor(post(urlEqualTo("/authorize"))
        .willReturn(aResponse().withFixedDelay(5_000)));   // beyond our read timeout

    var response = restTemplate.postForEntity("/orders", validRequest(), String.class);

    assertThat(response.getStatusCode()).isEqualTo(HttpStatus.SERVICE_UNAVAILABLE);
    assertThat(orderRepository.findAll()).isEmpty();
  }
}
```

The cases worth stubbing are the ones a real sandbox will not give you on demand: a timeout, a
500, a malformed body, a 429, a slow-then-recovering dependency. **The timeout case is the most
valuable test in this category** — it is the one that proves a read timeout is configured at
all, and a missing read timeout is a production incident waiting for a slow dependency.

Record the real downstream's responses once and use them as stub fixtures, so the stubs resemble
reality rather than your assumptions about it.

## Real databases, not in-memory ones

```java
@SpringBootTest
@Testcontainers
class OrderPersistenceIT {

  @Container
  @ServiceConnection
  static PostgreSQLContainer<?> postgres = new PostgreSQLContainer<>("postgres:16-alpine");
}
```

H2 standing in for Postgres passes tests that production fails. The dialects differ exactly
where it matters: upserts, JSON columns, row locking, sequence behaviour, and the text of
constraint-violation messages that error handling branches on. A unique-constraint test against
H2 can pass while the real constraint does not exist, because the migration was never applied.

`@ServiceConnection` (Boot 3.1+) wires the datasource automatically — older examples use
`@DynamicPropertySource`, which still works but is more to maintain. Declare the container
`static` so it is reused across the class rather than restarted per test.

## Test data

The main cause of a flaky API suite. Pick one strategy and hold it:

| Strategy | Trade-off |
|---|---|
| `@Transactional` rollback per test | fast, but hides commit-time behaviour and does not work across HTTP |
| truncate between tests | reliable, moderately fast, needs a known table list |
| fresh container per class | slowest, most isolated |
| unique data per test (random ids) | fast and parallel-safe, but leaves data behind |

For tests that go over HTTP, rollback does not apply — the request runs on another thread and
commits. Truncation or unique data are the real options there, and mixing strategies within one
suite is how order-dependent failures appear.

Never let one test depend on another's data. It passes in the order you wrote it and fails when
someone adds a test above.

## Keeping the suite fast and honest

- Reuse the Spring context. A differing `@MockitoBean` or property forces a fresh one; a few
  distinct configurations are fine, a dozen means minutes of startup.
- Reuse containers (`testcontainers.reuse.enable=true`) locally.
- Tag the slow tests so the fast ones can run pre-commit and everything runs in CI.
- Parallelise only once data isolation is actually per-test.
- Assert on behaviour, not timing. No `Thread.sleep`; use Awaitility with a condition for
  anything asynchronous.

A flaky test gets muted, and a muted test is worse than no test because it still looks like
coverage. When one is flaky, fix the cause or delete it — do not retry it.

## Consumer-driven contracts

For an API with several internal consumers, conformance against your own spec proves you match
what you *published*, not what consumers actually depend on. A consumer-driven contract (Pact,
Spring Cloud Contract) inverts that: each consumer declares what it uses, and your build fails
when you break any of them.

Worth the setup cost when breaking a consumer is expensive and they cannot all upgrade
together. Not worth it for a public API with unknown consumers, where a published spec plus
versioning is the practical answer.

## Reference files

- `references/test-recipes.md` — complete WireMock, Testcontainers and conformance setups,
  plus the pagination, idempotency and concurrency cases at API level.
