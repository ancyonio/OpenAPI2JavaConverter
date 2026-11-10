---
name: junit-mockito-test-design
description: >
  Design and write JUnit 5 and Mockito tests that actually catch regressions — unit tests,
  Spring slice tests, integration tests with Testcontainers — and reach a coverage target
  without gaming it. Use this whenever you write or review Java tests, whenever you are asked
  to raise coverage or "add tests" for a class or endpoint, whenever a test suite has high
  coverage but misses bugs, and whenever a mutation-testing score is low. Reach for it before
  writing the first test of a class, because the decisions that make a suite valuable — what to
  assert, what to mock, which cases are mandatory — are made then and are expensive to retrofit.
license: Apache-2.0
compatibility: >
  JUnit 5, Mockito and AssertJ on Java 17+. Spring sections assume Spring Boot 3.x/4.x test
  support (@WebMvcTest, @SpringBootTest, @MockitoBean). JaCoCo and PIT for the gates.
---

# Designing tests that catch things

There is one idea worth internalising before any technique:

**Coverage measures whether a line ran. It cannot tell a test that asserts the right outcome
from a test that asserts nothing.**

```java
@Test
void createOrder_works() {
  orderService.create(command);   // 100% line coverage of create()
}                                 // zero assertions
```

That test passes forever, covers everything, and catches nothing. It is also what you get
whenever coverage is the only thing being measured — and it is especially what you get from
AI-written test suites, because exercising code is the easy half and asserting the right
behaviour is the hard half.

So the question to carry through every test you write is not "is this line covered?" but
**"what change to the production code would make this test fail?"** If the answer is "none",
delete the test. It is costing maintenance and buying nothing.

## Deriving the cases you need

Do not start from the code and ask what is uncovered — that produces tests shaped like the
implementation, which pass after a refactor that broke the behaviour. Start from the
*contract* and derive the cases from the shape of the thing under test.

For a REST endpoint, the shape tells you the cases:

| The operation... | ...so you need |
|---|---|
| exists at all | happy path (unit), status + body (slice), response matches its schema |
| takes a request body | each constraint violated → 400 + problem body; malformed JSON → 400 |
| takes required params | missing → 400; wrong type → 400; boundary values |
| can return 404 | not-found → 404 + problem body |
| is a `POST` returning 201 | `Location` header set |
| can return 409 | conflict → 409 |
| declares `Idempotency-Key` | repeated key → same response, **one** side effect |
| is authenticated | unauthenticated → 401; wrong scope → 403 |
| is paginated | first page, last page, empty, beyond-end, invalid page size → 400 |
| uses `If-Match` | stale version → 412 |
| mutates state | repository interaction captured and asserted |
| calls a dependency | dependency 5xx → mapped error; timeout → mapped error |
| has an enum field | every value (parameterised); unknown value → 400 |

This list is mechanical enough to generate from an API description, which is worth doing if
you have one — then "write thorough tests" becomes "implement these seventeen named cases",
and whether you are done is checkable instead of a matter of opinion.
`references/required-cases.md` has the full derivation and how to make it verifiable.

## The test pyramid, and what each layer is for

| Layer | Tool | Mocks | What it proves |
|---|---|---|---|
| Unit | `@ExtendWith(MockitoExtension.class)` | all collaborators | business behaviour, branches, exceptions |
| Mapper | plain JUnit | none | field-by-field mapping, nulls, collections |
| Slice | `@WebMvcTest` + `@MockitoBean` | the service | status, headers, JSON body, validation |
| Integration | `@SpringBootTest` + Testcontainers | none | real wiring, transactions, persistence |
| Contract | `@SpringBootTest` | none | responses validate against the API description |
| Architecture | ArchUnit | none | layering, annotations, naming |

Two things not to test: generated DTOs (testing a generated getter measures the generator) and
framework behaviour (that `@Valid` works is Spring's test, not yours).

For integration tests, use Testcontainers rather than an in-memory database. H2 standing in for
Postgres passes tests that production fails, because the dialects differ exactly where it
matters — upserts, JSON columns, locking, constraint error messages.

## Mockito discipline

- **Strict stubs** (`MockitoExtension`'s default). When it complains about unnecessary
  stubbing, remove the stub — do not relax the strictness. The complaint is telling you the
  test sets up state the code never reads, which means the test does not test what you think.
- **Stub only what the test needs.** Every extra stub is an assumption about the
  implementation, and it is what makes a suite break on every refactor.
- **Never stub the class under test.** If you need to, the class is doing two things.
- **Never mock** value objects, records, DTOs, or `Clock`. Mocking a value object tests your
  mock. Inject a fixed `Clock` instead: `Clock.fixed(instant, ZoneOffset.UTC)`.
- `@Mock` fields, not `Mockito.mock(...)` in a field initialiser — the extension resets the
  former between tests.
- **`verify` what matters, not everything.** Use `ArgumentCaptor` to assert the payload a
  collaborator received. Over-verification makes tests brittle without making them stronger.
- **`verifyNoInteractions` is often the strongest assertion in the class.** "This path must not
  reach the payment gateway" is a real requirement, and nothing else expresses it.

## Writing the test itself

- Name: `methodName_stateUnderTest_expectedBehaviour`. The name should let a failure be
  understood from the report alone, without opening the file.
- AssertJ, not JUnit's bare `assertEquals` — the failure messages are the difference between
  diagnosing from CI output and reproducing locally.
- Arrange-Act-Assert, visibly separated by blank lines.
- One logical assertion per test. Group related checks on one object with
  `assertThat(x).satisfies(...)` or `SoftAssertions`, rather than splitting into tests that
  repeat the same arrange block.
- `@ParameterizedTest` for validation boundaries and enum coverage.
- Builders for fixtures (`OrderFixtures.createCommand().withTotal(...).build()`), so a test
  states only the field it cares about and stays readable when the DTO grows a field.
- **Deterministic**: no `Thread.sleep`, no real network, no system clock, no unseeded random,
  no dependence on execution order. A flaky test gets muted, and a muted test is worse than no
  test because it still looks like coverage.

`references/test-patterns.md` has worked examples of each layer.

## Coverage as a floor, and mutation score as the real gate

Reasonable thresholds: 85% line, 80% branch overall, 90% line on the service layer. Exclude
generated code, configuration and the application entry point — coverage of a generated getter
is noise in the denominator.

But treat those as a floor, not the goal, and add **mutation testing** (PIT) as the gate that
actually bites. PIT changes the production code — flips a conditional, replaces a return value
— and checks whether any test notices. That is precisely the property line coverage cannot see.
A threshold around 70% on the service layer is realistic.

**If line coverage is high and mutation score is low, the assertions are too weak.** The fix is
stronger assertions, not more tests. That diagnosis is the main practical reason to run PIT at
all: it turns "the tests feel thin" into a number with a list of surviving mutants attached.

Expect the mutation gate to be the one that fails. That is the gate working.

## Things that are never acceptable

Not because a rule forbids them, but because each one converts a quality signal into a lie:

- A test that calls a method and asserts nothing.
- `assertThat(x).isNotNull()` as the only assertion.
- Widening a coverage exclusion to drop an uncovered class.
- Lowering a threshold to make a build pass.
- `@Disabled` without a linked issue and a date.
- Asserting on log output as a substitute for asserting on behaviour.

If a class cannot be tested without changing it, say exactly what change is needed and why.
Do not weaken the test to fit untestable code — untestable code is a design defect, and
accommodating it hides the defect and keeps it.

## Reference files

- `references/required-cases.md` — deriving mandatory cases from an endpoint's shape, and
  making the derivation verifiable with test markers.
- `references/test-patterns.md` — worked unit, slice, integration, contract and parameterised
  tests, plus JaCoCo and PIT configuration.
