# Worked test patterns

One example per layer, each chosen to show an assertion that a weaker version of the same test
would miss.

## Contents

- [Service unit test](#service-unit-test)
- [API slice test](#api-slice-test)
- [Integration test](#integration-test)
- [Contract conformance test](#contract-conformance-test)
- [Fixture builders](#fixture-builders)
- [JaCoCo and PIT configuration](#jacoco-and-pit-configuration)

## Service unit test

```java
@ExtendWith(MockitoExtension.class)
class OrderServiceTest {

  private static final Instant NOW = Instant.parse("2026-11-09T10:15:30Z");

  @Mock private OrderRepository orderRepository;
  @Mock private PaymentClient paymentClient;
  @Spy private final Clock clock = Clock.fixed(NOW, ZoneOffset.UTC);

  @InjectMocks private OrderService orderService;

  // o2j:createOrder:unit:happy-path
  // o2j:createOrder:unit:repository-interaction-captured
  @Test
  void create_whenPaymentAuthorized_persistsOrderAsConfirmed() {
    var command = OrderFixtures.createCommand().withTotal(new BigDecimal("49.99")).build();
    when(paymentClient.authorize(any())).thenReturn(PaymentResult.authorized("auth-123"));
    when(orderRepository.save(any(Order.class))).thenAnswer(i -> i.getArgument(0));

    var created = orderService.create(command);

    var captor = ArgumentCaptor.forClass(Order.class);
    verify(orderRepository).save(captor.capture());
    assertThat(captor.getValue()).satisfies(saved -> {
      assertThat(saved.status()).isEqualTo(OrderStatus.CONFIRMED);
      assertThat(saved.authorizationId()).isEqualTo("auth-123");
      assertThat(saved.createdAt()).isEqualTo(NOW);
    });
    assertThat(created.status()).isEqualTo(OrderStatus.CONFIRMED);
  }

  // o2j:createOrder:unit:downstream-failure-mapped
  @Test
  void create_whenPaymentDeclined_throwsAndPersistsNothing() {
    var command = OrderFixtures.createCommand().build();
    when(paymentClient.authorize(any())).thenReturn(PaymentResult.declined("insufficient_funds"));

    assertThatThrownBy(() -> orderService.create(command))
        .isInstanceOf(PaymentDeclinedException.class)
        .hasMessageContaining("insufficient_funds");

    verify(orderRepository, never()).save(any());
  }

  // o2j:createOrder:unit:validation-failure
  @ParameterizedTest
  @ValueSource(strings = {"0.00", "-1.00"})
  void create_whenTotalNotPositive_rejectsBeforeCallingPayment(String total) {
    var command = OrderFixtures.createCommand().withTotal(new BigDecimal(total)).build();

    assertThatThrownBy(() -> orderService.create(command))
        .isInstanceOf(InvalidOrderException.class);

    verifyNoInteractions(paymentClient, orderRepository);
  }

  @Test
  void create_whenIdempotencyKeyReplayed_returnsSameOrderAndSavesOnce() {
    var command = OrderFixtures.createCommand().withIdempotencyKey("key-1").build();
    var existing = OrderFixtures.confirmed("ord-1");
    when(orderRepository.findByIdempotencyKey("key-1")).thenReturn(Optional.of(existing));

    var result = orderService.create(command);

    assertThat(result).isEqualTo(existing);
    verify(orderRepository, never()).save(any());
    verifyNoInteractions(paymentClient);
  }
}
```

Three assertions here would be missing from a weaker version, and each is the one that catches
a real bug:

- `verify(orderRepository, never()).save(any())` in the declined case — proves no partial
  write. A test asserting only the exception passes against an implementation that saves first
  and charges second.
- `verifyNoInteractions(paymentClient, orderRepository)` in the validation case — proves
  *ordering*: validation happens before the downstream call. This is the kind of property
  mutation testing rewards, because flipping the order is a mutant that survives otherwise.
- `verifyNoInteractions(paymentClient)` in the replay case — proves the side effect happened
  once, which is the whole point of idempotency.

## API slice test

```java
@WebMvcTest(controllers = OrdersApiController.class)
@Import({OrdersApiDelegateImpl.class, OrderMapperImpl.class, GlobalExceptionHandler.class})
class OrdersApiControllerTest {

  @Autowired private MockMvc mockMvc;
  @MockitoBean private OrderService orderService;

  // o2j:createOrder:slice:201-location-header
  @Test
  void createOrder_whenValid_returns201WithLocationHeader() throws Exception {
    when(orderService.create(any())).thenReturn(OrderFixtures.confirmed("ord-1"));

    mockMvc.perform(post("/orders")
            .header("Idempotency-Key", "key-12345678")
            .contentType(MediaType.APPLICATION_JSON)
            .content("""
                {"customerId":"cust-1","items":[{"sku":"SKU-0001","quantity":2}]}
                """))
        .andExpect(status().isCreated())
        .andExpect(header().string("Location", "/orders/ord-1"))
        .andExpect(jsonPath("$.id").value("ord-1"))
        .andExpect(jsonPath("$.status").value("CONFIRMED"));
  }

  // o2j:createOrder:slice:400-problem-details
  @Test
  void createOrder_whenQuantityBelowMinimum_returns400ProblemDetail() throws Exception {
    mockMvc.perform(post("/orders")
            .header("Idempotency-Key", "key-12345678")
            .contentType(MediaType.APPLICATION_JSON)
            .content("""
                {"customerId":"cust-1","items":[{"sku":"SKU-0001","quantity":0}]}
                """))
        .andExpect(status().isBadRequest())
        .andExpect(content().contentTypeCompatibleWith("application/problem+json"))
        .andExpect(jsonPath("$.type").exists())
        .andExpect(jsonPath("$.title").value("Validation failed"))
        .andExpect(jsonPath("$.errors[0].field").value("items[0].quantity"));

    verifyNoInteractions(orderService);
  }

  // o2j:createOrder:slice:missing-required-param-400
  @Test
  void createOrder_whenIdempotencyKeyMissing_returns400() throws Exception {
    mockMvc.perform(post("/orders")
            .contentType(MediaType.APPLICATION_JSON)
            .content("""
                {"customerId":"cust-1","items":[{"sku":"SKU-0001","quantity":1}]}
                """))
        .andExpect(status().isBadRequest())
        .andExpect(content().contentTypeCompatibleWith("application/problem+json"));

    verifyNoInteractions(orderService);
  }
}
```

The `verifyNoInteractions(orderService)` on each failure path is what distinguishes a slice
test that checks the status from one that checks the *boundary* actually rejected the request.

## Integration test

```java
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@Testcontainers
class OrderPersistenceIT {

  @Container
  @ServiceConnection
  static PostgreSQLContainer<?> postgres = new PostgreSQLContainer<>("postgres:16-alpine");

  @Autowired private OrderService orderService;
  @Autowired private OrderRepository orderRepository;

  @Test
  void create_whenIdempotencyKeyDuplicated_violatesUniqueConstraintNotSilentDuplicate() {
    var first = OrderFixtures.createCommand().withIdempotencyKey("key-1").build();
    orderService.create(first);

    // The real constraint only exists in the migration, so only a real database proves it.
    assertThat(orderRepository.findAll()).hasSize(1);
  }
}
```

Use a real Postgres, not H2. H2 standing in for Postgres passes tests that production fails,
because the dialects differ exactly where it matters — upserts, JSON columns, locking, and the
text of constraint violations that error handling branches on.

`@ServiceConnection` (Boot 3.1+) wires the datasource automatically, replacing the
`@DynamicPropertySource` block older examples use.

## Contract conformance test

This is the test that makes the API description authoritative rather than decorative:

```java
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
class OpenApiContractConformanceTest {

  @Autowired private TestRestTemplate restTemplate;
  private static OpenApiValidator validator;

  @BeforeAll
  static void loadSpec() {
    validator = OpenApiValidator.from(Path.of("openapi/spec.canonical.yaml"));
  }

  // One test may claim a case for several operations: one marker per operation.
  // o2j:createOrder:contract:response-schema
  // o2j:getOrder:contract:response-schema
  @ParameterizedTest
  @MethodSource("operationSamples")
  void everyResponse_conformsToDeclaredSchema(OperationSample sample) {
    var response = restTemplate.exchange(
        sample.path(), sample.method(), sample.entity(), String.class);

    assertThat(validator.validate(sample.operationId(), response))
        .as("response for %s must match the declared schema", sample.operationId())
        .isValid();
  }
}
```

Without this, the description and the implementation drift and only a consumer finds out.

## Fixture builders

```java
public final class OrderFixtures {

  public static CreateOrderCommandBuilder createCommand() {
    return new CreateOrderCommandBuilder()
        .withCustomerId("cust-1")
        .withTotal(new BigDecimal("10.00"))
        .withItems(List.of(new OrderItem("SKU-0001", 1)));
  }

  public static Order confirmed(String id) {
    return new Order(id, "cust-1", OrderStatus.CONFIRMED, ...);
  }

  private OrderFixtures() {}
}
```

A builder with sensible defaults lets each test state only the field it cares about. That is
what keeps a test readable when the DTO grows a field — and what stops a new required field
from breaking fifty tests at once.

## JaCoCo and PIT configuration

```xml
<plugin>
  <groupId>org.jacoco</groupId>
  <artifactId>jacoco-maven-plugin</artifactId>
  <version>0.8.13</version>
  <configuration>
    <excludes>
      <exclude>**/api/model/**</exclude>          <!-- generated DTOs -->
      <exclude>**/api/*Api.class</exclude>
      <exclude>**/api/*ApiController.class</exclude>
      <exclude>**/Application.class</exclude>
      <exclude>**/config/**</exclude>
    </excludes>
  </configuration>
  <executions>
    <execution><goals><goal>prepare-agent</goal></goals></execution>
    <execution>
      <id>check</id>
      <phase>verify</phase>
      <goals><goal>check</goal></goals>
      <configuration>
        <rules>
          <rule>
            <element>BUNDLE</element>
            <limits>
              <limit><counter>LINE</counter><value>COVEREDRATIO</value><minimum>0.85</minimum></limit>
              <limit><counter>BRANCH</counter><value>COVEREDRATIO</value><minimum>0.80</minimum></limit>
            </limits>
          </rule>
          <rule>
            <element>PACKAGE</element>
            <includes><include>*.service.*</include></includes>
            <limits>
              <limit><counter>LINE</counter><value>COVEREDRATIO</value><minimum>0.90</minimum></limit>
            </limits>
          </rule>
        </rules>
      </configuration>
    </execution>
  </executions>
</plugin>

<plugin>
  <groupId>org.pitest</groupId>
  <artifactId>pitest-maven</artifactId>
  <version>1.17.4</version>
  <dependencies>
    <dependency>
      <groupId>org.pitest</groupId>
      <artifactId>pitest-junit5-plugin</artifactId>
      <version>1.2.1</version>
    </dependency>
  </dependencies>
  <configuration>
    <targetClasses><param>com.example.orders.service.*</param></targetClasses>
    <targetTests><param>com.example.orders.service.*Test</param></targetTests>
    <mutationThreshold>70</mutationThreshold>
    <coverageThreshold>85</coverageThreshold>
    <timestampedReports>false</timestampedReports>
    <outputFormats>
      <outputFormat>XML</outputFormat>
      <outputFormat>HTML</outputFormat>
    </outputFormats>
  </configuration>
</plugin>
```

Two notes on the PIT configuration. Scope it to the service layer: PIT runs the suite once per
mutant, so pointing it at the whole tree makes it too slow to keep. And set
`timestampedReports=false` — a timestamped directory name makes the output
non-reproducible, which matters if anything downstream reads the report.

## Reading a PIT report

Each surviving mutant names a line and the change that was made. Translate it:

| Surviving mutant | Usually means |
|---|---|
| `replaced return value with null` | nothing asserts the returned object |
| `negated conditional` | only one branch is exercised, or neither outcome is asserted |
| `removed call to ...` | a side effect is not verified |
| `replaced int return with 0` | a count or id is returned but never checked |
| `incremented/decremented` | a boundary is not tested |

A mutant that survives because the mutated code genuinely has no observable effect is dead
code. That is also worth knowing.
