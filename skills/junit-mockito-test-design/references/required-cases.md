# Deriving required cases, and making the derivation verifiable

"Write thorough tests" is not a specification. It produces whatever the author thought of, and
nobody can tell afterwards whether it was enough. Deriving the cases from the contract makes
the requirement explicit and the completeness checkable.

## The derivation

Each row is a feature of the operation, and each feature implies cases. Read the operation's
definition and collect every row that applies.

### Always

| Case | Layer | What it proves |
|---|---|---|
| `happy-path` | unit | the business rule holds |
| `happy-path-status` | slice | the declared success status and body shape |
| `response-schema` | contract | the response validates against the API description |

### Has a request body

| Case | Layer |
|---|---|
| `validation-failure` | unit |
| `400-problem-details` | slice |
| `malformed-json-400` | slice |

For `400-problem-details`, assert the problem body's `type`, `title` and the field-level
`errors[].field` — and assert `verifyNoInteractions(service)`, which catches a controller
doing work before validation runs.

### Has required path or query parameters

| Case | Layer |
|---|---|
| `missing-required-param-400` | slice |
| `wrong-param-type-400` | slice |
| boundary values for each numeric or length-constrained parameter | slice, parameterised |

### Status-specific

| If the operation declares | Case | Layer |
|---|---|---|
| 404 | `404-problem-details` | slice |
| 409 | `conflict-409` | unit |
| 412 | `stale-if-match-412` | slice |
| 201 | `201-location-header` | slice |

### Behavioural features

| Feature | Case | Layer |
|---|---|---|
| `Idempotency-Key` header | `idempotent-replay-single-side-effect` | unit |
| authenticated | `unauthenticated-401`, `wrong-scope-403` | slice |
| paginated | `pagination-first-page`, `pagination-last-page`, `pagination-empty`, `pagination-beyond-end`, `invalid-page-size-400` | slice |
| mutates state | `repository-interaction-captured` | unit |
| calls a dependency | `downstream-failure-mapped`, `downstream-timeout-mapped` | unit |
| has an enum field | `enum-all-values:<field>` (parameterised), `unknown-enum-value-400` | unit + slice |

The idempotency case is the one most often skipped and most worth having: it must assert both
that the second call returns the same response *and* that the side effect happened once. A test
asserting only the response passes against a broken implementation that charges twice.

## Making it verifiable

A derived list is only useful if you can tell whether it has been implemented. Inferring that
from test method names is guesswork. A marker comment is explicit and greppable, and names
the operation as well as the case - `o2j:<operationId>:<layer>:<case>`:

```java
// o2j:createOrder:slice:201-location-header
@Test
void createOrder_whenValid_returns201WithLocationHeader() throws Exception {
  ...
}
```

A marker counts only when all three hold, and `o2j verify --mode contract` checks each:

- it names the operation. Without that, one `unit:happy-path` comment satisfied the case for
  every operation in the spec;
- it sits on a JUnit test method - in its body, or directly above it with only annotations
  and comments in between. A marker in a helper method or a stray comment counts for nothing;
- that test ran and passed in the last `mvn verify`, according to the Surefire/Failsafe XML
  reports. `--static` skips this while tests are still being written; the gate never does.

Then a check can scan the test sources for markers and compare against the derived list:

```python
import re, pathlib

MARKER = re.compile(r"o2j:([a-z]+:[A-Za-z0-9\-:_]+)")

claimed = set()
for path in pathlib.Path("src/test/java").rglob("*.java"):
    claimed |= set(MARKER.findall(path.read_text(encoding="utf-8")))

missing = [c for c in required_cases if c not in claimed]
```

A missing marker means either the test was not written, or it was written without declaring
what it covers. Both need attention, and neither is visible in a coverage report.

Pick a prefix that suits the project; the mechanism matters, not the spelling. Keep the marker
on the test method, not the class, so that deleting a method removes its claim.

## Worked example

`POST /orders`, which: takes a `CreateOrderRequest` body, requires an `Idempotency-Key`
header, requires the `orders:write` scope, returns `201` with a `Location` header, declares
`400`, `401`, `403`, `409`, `500`, persists an order, calls a payment client, and has a
`status` enum field.

Applying the table:

```
unit:happy-path
slice:happy-path-status
contract:response-schema
unit:validation-failure
slice:400-problem-details
slice:malformed-json-400
slice:201-location-header
unit:conflict-409
unit:idempotent-replay-single-side-effect
slice:unauthenticated-401
slice:wrong-scope-403
unit:repository-interaction-captured
unit:downstream-failure-mapped
unit:downstream-timeout-mapped
unit:enum-all-values:status
slice:unknown-enum-value-400
```

Sixteen cases, every one traceable to a declared feature of the operation. None of them is a
judgement call, and none of them is "whatever the author thought of".

Compare that with what "write tests for createOrder" typically produces: a happy path, a
validation failure, and maybe a 404 that does not apply. The gap is not laziness — it is that
nobody enumerated the contract first.

## Keeping the list honest

Two failure modes to watch for.

**Cases that always pass.** If a case passes both with and without the behaviour it is
supposed to check, it is not discriminating and should be strengthened or removed. Mutation
testing finds these: a case whose mutants all survive is asserting nothing useful.

**Cases that are mechanically satisfied.** A marker on a test that does not actually cover the
case is worse than a missing marker, because it reports completeness falsely. If you are
generating or reviewing these, spot-check that the marker matches the assertions — particularly
for the pagination and idempotency cases, where a weak version is easy to write.
