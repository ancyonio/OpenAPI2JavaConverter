# API test recipes

Complete setups for the things that take longest to get right, plus the API-level cases that
are usually missing.

## Contents

- [Testcontainers](#testcontainers) · [WireMock](#wiremock) · [Conformance](#conformance)
- [Pagination at the API](#pagination-at-the-api)
- [Idempotency](#idempotency) · [Optimistic concurrency](#optimistic-concurrency)
- [Security cases](#security-cases)
- [Asserting the absence of things](#asserting-the-absence-of-things)
- [Keeping it fast](#keeping-it-fast)

## Testcontainers

```java
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@Testcontainers
abstract class AbstractIntegrationTest {

  // static: started once per JVM and shared, not per test class
  @Container
  @ServiceConnection
  static PostgreSQLContainer<?> postgres = new PostgreSQLContainer<>("postgres:16-alpine");

  @Autowired protected TestRestTemplate restTemplate;

  @AfterEach
  void truncate(@Autowired JdbcTemplate jdbc) {
    // Rollback does not work for tests that go over HTTP: the request runs on another
    // thread and commits. Truncation is the reliable option there.
    jdbc.execute("truncate table order_item, orders cascade");
  }
}
```

Pin the image tag. `postgres:latest` means a test suite that changes behaviour without a commit.

Enable reuse locally to avoid a container start per run:

```properties
# ~/.testcontainers.properties
testcontainers.reuse.enable=true
```

Do not enable reuse in CI — a leftover container carries state between builds.

## WireMock

```java
@SpringBootTest(webEnvironment = RANDOM_PORT)
@AutoConfigureWireMock(port = 0)        // random port, injected as wiremock.server.port
class PaymentGatewayTest extends AbstractIntegrationTest {

  @Test
  void createOrder_whenGatewayReturns500_mapsTo502AndPersistsNothing() {
    stubFor(post(urlPathEqualTo("/authorize"))
        .willReturn(aResponse().withStatus(500)));

    var response = restTemplate.postForEntity("/orders", validRequest(), String.class);

    assertThat(response.getStatusCode()).isEqualTo(HttpStatus.BAD_GATEWAY);
    assertThat(orderRepository.count()).isZero();
  }

  @Test
  void createOrder_whenGatewayExceedsReadTimeout_failsCleanly() {
    // Our read timeout is 5s. This is the test that proves it is configured at all.
    stubFor(post(urlPathEqualTo("/authorize"))
        .willReturn(aResponse().withFixedDelay(10_000)));

    var start = System.nanoTime();
    var response = restTemplate.postForEntity("/orders", validRequest(), String.class);
    var elapsed = Duration.ofNanos(System.nanoTime() - start);

    assertThat(response.getStatusCode()).isEqualTo(HttpStatus.GATEWAY_TIMEOUT);
    // The assertion that matters: we gave up rather than waiting on the dependency.
    assertThat(elapsed).isLessThan(Duration.ofSeconds(8));
  }

  @Test
  void createOrder_whenGatewayReturnsMalformedBody_doesNotLeakTheParseError() {
    stubFor(post(urlPathEqualTo("/authorize"))
        .willReturn(okJson("{ this is not json")));

    var response = restTemplate.postForEntity("/orders", validRequest(), String.class);

    assertThat(response.getStatusCode().is5xxServerError()).isTrue();
    assertThat(response.getBody()).doesNotContain("Unexpected character");
  }

  @Test
  void createOrder_sendsTheCorrelationIdDownstream() {
    stubFor(post(urlPathEqualTo("/authorize")).willReturn(okJson(authorized())));

    var headers = new HttpHeaders();
    headers.set("X-Correlation-ID", "test-correlation-1");
    restTemplate.postForEntity("/orders", new HttpEntity<>(validRequest(), headers), String.class);

    verify(postRequestedFor(urlPathEqualTo("/authorize"))
        .withHeader("X-Correlation-ID", equalTo("test-correlation-1")));
  }
}
```

The timeout test is the single most valuable test in this file. A missing read timeout cannot be
found by reading code — it looks like an absent line — and it is what takes a service down when
a dependency goes slow rather than down.

Capture real responses once (WireMock's record mode) and keep them as fixtures, so stubs
resemble the dependency rather than your assumptions about it.

## Conformance

```java
@SpringBootTest(webEnvironment = RANDOM_PORT)
class OpenApiConformanceTest extends AbstractIntegrationTest {

  private static OpenApiInteractionValidator validator;

  @BeforeAll
  static void loadSpec() {
    validator = OpenApiInteractionValidator
        .createForSpecificationUrl("openapi/spec.yaml")
        .withWhitelist(ValidationErrorsWhitelist.create()
            // Narrow and justified, like any suppression.
            .withRule("ignore additionalProperties on the free-form metadata map",
                WhitelistRules.messageContains("additionalProperties")))
        .build();
  }

  @ParameterizedTest
  @MethodSource("everyDeclaredOperation")   // driven from the spec, not a hand-written list
  void response_matchesDeclaredSchema(OperationSample sample) {
    var response = restTemplate.exchange(
        sample.path(), sample.method(), sample.entity(), String.class);

    var report = validator.validateResponse(
        sample.pathTemplate(), sample.method(), toValidatableResponse(response));

    assertThat(report.hasErrors()).as("%s -> %s", sample.operationId(), report).isFalse();
  }
}
```

Drive the parameters from the spec's own operation list. A conformance suite built from a
hand-written list proves nothing about the operations nobody remembered.

Validate error responses too — they are the schemas that drift, because nobody looks at them.

## Pagination at the API

```java
@Test
void listOrders_firstPage_returnsPageMetadataAndLink() throws Exception {
  givenOrders(25);

  mockMvc.perform(get("/orders?page=0&size=10"))
      .andExpect(status().isOk())
      .andExpect(jsonPath("$.content", hasSize(10)))
      .andExpect(jsonPath("$.page").value(0))
      .andExpect(jsonPath("$.totalElements").value(25));
}

@Test
void listOrders_beyondLastPage_returnsEmptyNotError() throws Exception {
  givenOrders(5);

  mockMvc.perform(get("/orders?page=99&size=10"))
      .andExpect(status().isOk())
      .andExpect(jsonPath("$.content", hasSize(0)));   // 200 with an empty page, not 404
}

@Test
void listOrders_whenSizeExceedsMaximum_returns400() throws Exception {
  mockMvc.perform(get("/orders?page=0&size=10000"))
      .andExpect(status().isBadRequest())
      .andExpect(content().contentTypeCompatibleWith("application/problem+json"));
}

@Test
void listOrders_pagesDoNotOverlap() throws Exception {
  givenOrders(20);
  var first = idsFrom(mockMvc.perform(get("/orders?page=0&size=10")));
  var second = idsFrom(mockMvc.perform(get("/orders?page=1&size=10")));

  // Fails when the sort key is not unique - the classic duplicate-row-across-pages bug.
  assertThat(first).doesNotContainAnyElementsOf(second);
}
```

That last test is the one teams skip and then hit in production: a non-unique sort key lets the
same row appear on two pages, and no single-page test can see it.

## Idempotency

```java
@Test
void createOrder_replayedIdempotencyKey_returnsSameResponseAndChargesOnce() {
  stubFor(post(urlPathEqualTo("/authorize")).willReturn(okJson(authorized())));
  var headers = new HttpHeaders();
  headers.set("Idempotency-Key", "key-00000001");

  var first = restTemplate.postForEntity("/orders", new HttpEntity<>(validRequest(), headers), OrderResponse.class);
  var second = restTemplate.postForEntity("/orders", new HttpEntity<>(validRequest(), headers), OrderResponse.class);

  assertThat(second.getBody().id()).isEqualTo(first.getBody().id());
  assertThat(orderRepository.count()).isEqualTo(1);
  verify(exactly(1), postRequestedFor(urlPathEqualTo("/authorize")));   // charged once
}

@Test
void createOrder_concurrentSameKey_createsExactlyOne() throws Exception {
  stubFor(post(urlPathEqualTo("/authorize")).willReturn(okJson(authorized())));
  var headers = new HttpHeaders();
  headers.set("Idempotency-Key", "key-00000002");
  var body = new HttpEntity<>(validRequest(), headers);

  var pool = Executors.newFixedThreadPool(2);
  var a = pool.submit(() -> restTemplate.postForEntity("/orders", body, String.class));
  var b = pool.submit(() -> restTemplate.postForEntity("/orders", body, String.class));
  a.get(); b.get();

  assertThat(orderRepository.count()).isEqualTo(1);
}
```

Both assertions are needed. Checking only the response passes against an implementation that
charges twice; the concurrency case is what catches a check-then-act race that a sequential test
cannot.

## Optimistic concurrency

```java
@Test
void updateOrder_withStaleIfMatch_returns412() throws Exception {
  var created = createOrder();
  var staleEtag = created.getHeaders().getETag();
  updateOrder(created, "first change");          // bumps the version

  mockMvc.perform(put("/orders/" + idOf(created))
          .header("If-Match", staleEtag)
          .contentType(MediaType.APPLICATION_JSON)
          .content(updateBody("second change")))
      .andExpect(status().isPreconditionFailed());
}
```

Without this, last-write-wins silently erases one user's edit — a data-loss bug with no error
anywhere.

## Security cases

```java
@Test
void getOrder_withNoToken_returns401() throws Exception {
  mockMvc.perform(get("/orders/1"))
      .andExpect(status().isUnauthorized())
      .andExpect(content().contentTypeCompatibleWith("application/problem+json"));
}

@Test
void createOrder_withReadScopeOnly_returns403() throws Exception {
  mockMvc.perform(post("/orders")
          .with(jwt().authorities(new SimpleGrantedAuthority("SCOPE_orders:read")))
          .contentType(MediaType.APPLICATION_JSON)
          .content(validBody()))
      .andExpect(status().isForbidden());
}
```

The 403 test is the one that proves the scope check exists rather than merely compiles. Use
`jwt()` rather than `@WithMockUser`, which does not exercise the resource server's claim
conversion at all.

## Asserting the absence of things

Easy to forget, and these are the assertions that stop a future refactor reintroducing a leak:

```java
@Test
void internalFailure_leaksNothingAboutItself() throws Exception {
  when(orderService.create(any())).thenThrow(new IllegalStateException("jdbc connection refused to db-prod-07"));

  var body = mockMvc.perform(post("/orders").contentType(APPLICATION_JSON).content(validBody()))
      .andExpect(status().isInternalServerError())
      .andReturn().getResponse().getContentAsString();

  assertThat(body)
      .doesNotContain("jdbc", "db-prod-07", "IllegalStateException")
      .contains("correlationId");
}
```

## Keeping it fast

- One shared `@SpringBootTest` configuration, so the context is cached. A differing
  `@MockitoBean` or property forces a new one; a dozen variants means minutes of startup.
- `static` containers, reused across classes.
- Tag the slow tests (`@Tag("integration")`) so the fast ones run pre-commit.
- Parallelise only once data isolation is per-test. Shared mutable fixtures plus parallelism
  gives failures that depend on scheduling.
- No `Thread.sleep`. For anything asynchronous, Awaitility with a condition and a timeout.
