---
name: service-resilience-observability
description: >
  Make a service survive its dependencies and be diagnosable when it does not — timeouts,
  retries, circuit breakers, bulkheads, idempotency, graceful shutdown, structured logging,
  correlation ids, metrics and tracing. Use this whenever an outbound HTTP or database call is
  added or reviewed; whenever a slow or failing dependency has taken a service down; whenever
  an incident could not be diagnosed from the logs; whenever retries, backoff or a circuit
  breaker are being considered; and whenever you are deciding what to log, measure or trace.
  Reach for it when adding any call to something you do not control, because a missing read
  timeout is the single most common cause of a service failing in a way its own tests cannot
  reproduce.
license: Apache-2.0
compatibility: >
  Spring Boot 3.x/4.x with Actuator and Micrometer, Java 17+. Resilience examples use
  Resilience4j; the reasoning applies to any equivalent.
---

# Surviving dependencies, and being diagnosable

Two properties, one reason to treat them together: when a dependency misbehaves, resilience
decides whether you stay up, and observability decides whether you can tell why. Having one
without the other means either falling over mysteriously or staying up mysteriously.

If you take one thing from this: **every outbound call needs a connect timeout and a read
timeout.** A missing read timeout is the most common cause of a service failing in a way its own
test suite cannot reproduce — the dependency goes slow rather than down, threads pile up on
calls that never return, the pool exhausts, health checks fail, and the service is gone. The
dependency never returned an error, so nothing in the logs looks like a failure.

## Timeouts

```java
@Bean
RestClient paymentClient(RestClient.Builder builder) {
  var factory = new SimpleClientHttpRequestFactory();
  factory.setConnectTimeout(Duration.ofSeconds(2));
  factory.setReadTimeout(Duration.ofSeconds(5));    // the one that is forgotten
  return builder.baseUrl("https://payments.example.com")
      .requestFactory(factory)
      .build();
}
```

Set them everywhere, not just on HTTP clients: database statement timeouts, connection-pool
acquisition timeouts, message broker calls, cache lookups.

Pick the value from the dependency's actual latency, not a round number — roughly its p99 plus
headroom. A timeout longer than your own caller's timeout is pointless; a timeout longer than
the whole request budget is worse, because you hold resources for a response nobody will
receive. Budget from the outside in: if the caller gives up at 3 seconds, no downstream call
may take 5.

## Retries

Retry only what is safe to repeat. A retried non-idempotent `POST` charges the customer twice,
and that is a worse outcome than the original error.

| Safe | Not safe without an idempotency key |
|---|---|
| `GET`, `HEAD`, `PUT`, `DELETE` | `POST`, `PATCH` |
| a connect timeout (nothing was sent) | a read timeout (it may have succeeded) |
| 429, 503 with `Retry-After` | 400, 401, 403, 404, 409, 422 |

A read timeout is the subtle one: the request may have been processed and only the response
lost. Retrying it is a duplicate unless the operation is idempotent.

Always exponential backoff **with jitter**. Fixed-interval retries from many instances
synchronise into a thundering herd that keeps a recovering dependency down.

And retry at one level only. Client retries nested inside service retries inside a gateway's
retries multiply: three at each of three levels is twenty-seven requests from one call.

## Circuit breakers

A circuit breaker stops you queuing work for something that is already failing. Without one,
retries make an outage worse.

```java
@CircuitBreaker(name = "payments", fallbackMethod = "authorizeUnavailable")
@Retry(name = "payments")
public PaymentResult authorize(PaymentRequest request) { ... }

private PaymentResult authorizeUnavailable(PaymentRequest request, Throwable cause) {
  // Fail fast and explicitly. A fallback must never fabricate a success: returning
  // "authorized" because the gateway is down ships goods that were never paid for.
  log.warn("payment gateway unavailable, failing fast", cause);
  throw new PaymentUnavailableException(cause);
}
```

Order matters: the breaker wraps the retry, so a burst of retries counts as one failure rather
than resetting the window.

The fallback rule is the important one — degrade or fail, never invent. A cached or default
value is fine when staleness is acceptable and visible. A fabricated success is a correctness
bug dressed as resilience.

Add a **bulkhead** (a bounded pool per dependency) so one slow dependency cannot consume every
thread. That is what stops a failure in a non-critical dependency taking out the whole service.

## Idempotency

Where the contract declares an `Idempotency-Key`, honour it: store the key with the result, and
on a repeat return the stored response without re-executing.

The test that matters asserts **both** that the second call returns the same response and that
the side effect happened once. A test checking only the response passes against an
implementation that charges twice.

Mind the race: two concurrent requests with the same key must not both proceed. A unique
constraint on the key column is the simplest correct answer — let the database arbitrate.

## Structured logging

JSON, not prose, so logs are queryable. And decide deliberately what goes in:

**Never logged:** credentials, tokens, `Authorization` headers, card numbers, personal data,
request bodies that might contain any of the above.

**Always available:** a correlation id, so one caller's journey can be followed across services
and so a 500's `correlationId` leads to the line that explains it.

```java
MDC.put("correlationId", correlationId);
try {
  chain.doFilter(request, response);
} finally {
  MDC.remove(correlationId);     // or it leaks to the next request on a pooled thread
}
```

Accept an inbound `X-Correlation-ID` and generate one otherwise — but validate it. It is
caller-controlled and ends up in your log lines, so cap its length and reject blanks, or a
caller can inject newlines and forge log entries.

Log levels that stay useful: `ERROR` for something needing a human, `WARN` for a handled
degradation, `INFO` for state changes worth an audit trail, `DEBUG` off in production. If
everything is `ERROR`, nothing is.

## Metrics

Instrument the service layer, not just HTTP. Four that earn their place:

- **Rate, errors, duration** per operation — percentiles, not averages. An average hides the
  tail, and the tail is the user experience.
- **Dependency call** duration and failure rate per dependency, so an incident points at a
  cause.
- **Circuit breaker state** and **bulkhead saturation** — the leading indicators.
- **Business counters** for the things you would be asked about in an incident: orders created,
  payments declined.

```java
@Timed(value = "order.create", percentiles = {0.5, 0.95, 0.99})
public Order create(CreateOrderCommand command) { ... }
```

Keep tag cardinality bounded. A tag carrying a customer id or an order id creates a time series
per value and will take down the metrics backend.

## Health and shutdown

Separate liveness from readiness, because they answer different questions: liveness is "should
this be restarted", readiness is "should this receive traffic". Conflating them means a pod
restarted while it is merely waiting for a dependency.

A dependency check belongs in readiness, not liveness — otherwise a downstream blip restarts
your whole fleet.

```yaml
server:
  shutdown: graceful
spring:
  lifecycle:
    timeout-per-shutdown-phase: 30s
management:
  endpoint:
    health:
      probes:
        enabled: true
```

Graceful shutdown lets in-flight requests finish. Without it every deployment fails a few
requests, and that noise trains everyone to ignore error-rate alerts.

## Tracing

Distributed tracing earns its cost once a request crosses more than two services. Propagate
context (W3C `traceparent`), sample at a rate you can afford, and make sure the trace id is in
the logs too — a trace showing a slow span and logs you cannot correlate to it leaves you
halfway.

## Testing all of this

The resilience behaviour is testable, and untested resilience code is usually wrong:

- Stub a downstream that **delays beyond your read timeout** and assert you fail cleanly. This
  is the test that proves the timeout exists.
- Stub repeated failures and assert the breaker opens, then that it recovers.
- Assert a retried operation is idempotent: one side effect after two attempts.
- Assert the correlation id appears in the response and propagates to the downstream request.
- Assert no secret appears in a log line, for the paths that handle them.
