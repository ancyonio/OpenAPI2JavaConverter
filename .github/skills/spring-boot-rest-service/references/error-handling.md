# RFC 9457 error handling, end to end

One advice class, one error shape, and nothing internal in the response body.

## The domain exception hierarchy

The service layer throws these. It does not know about HTTP; the status lives on the exception
so the advice needs no `instanceof` ladder.

```java
public abstract class DomainException extends RuntimeException {

  private final String code;

  protected DomainException(String code, String message) {
    super(message);
    this.code = code;
  }

  /** Stable, machine-readable code; becomes the `type` URI in the ProblemDetail. */
  public String code() {
    return code;
  }

  public abstract HttpStatus status();
}

public class NotFoundException extends DomainException {
  public NotFoundException(String resource, Object id) {
    super("not-found", "%s %s was not found".formatted(resource, id));
  }

  @Override public HttpStatus status() { return HttpStatus.NOT_FOUND; }
}

public class ConflictException extends DomainException {
  public ConflictException(String message) {
    super("conflict", message);
  }

  @Override public HttpStatus status() { return HttpStatus.CONFLICT; }
}

public class PreconditionFailedException extends DomainException {
  public PreconditionFailedException(String message) {
    super("stale-version", message);
  }

  @Override public HttpStatus status() { return HttpStatus.PRECONDITION_FAILED; }
}
```

A domain message is safe to return because you wrote it for a caller. An *unexpected*
exception's message is not — see the last handler below.

## The advice

```java
@RestControllerAdvice
public class GlobalExceptionHandler {

  private static final Logger log = LoggerFactory.getLogger(GlobalExceptionHandler.class);
  private static final String PROBLEM_BASE = "https://problems.example.com/orders/";
  private static final String MDC_CORRELATION_ID = "correlationId";

  @ExceptionHandler(DomainException.class)
  ProblemDetail onDomain(DomainException exception) {
    log.info("domain failure code={} status={}", exception.code(), exception.status().value());
    return problem(exception.status(), exception.code(), exception.getMessage());
  }

  @ExceptionHandler(MethodArgumentNotValidException.class)
  ProblemDetail onInvalidBody(MethodArgumentNotValidException exception) {
    List<Map<String, String>> errors =
        exception.getBindingResult().getFieldErrors().stream()
            .map(e -> Map.of(
                "field", e.getField(),
                "message", String.valueOf(e.getDefaultMessage())))
            .toList();

    ProblemDetail problem =
        problem(HttpStatus.BAD_REQUEST, "validation-failed", "Validation failed");
    problem.setTitle("Validation failed");
    problem.setProperty("errors", errors);
    return problem;
  }

  @ExceptionHandler(ConstraintViolationException.class)
  ProblemDetail onConstraintViolation(ConstraintViolationException exception) {
    List<Map<String, String>> errors =
        exception.getConstraintViolations().stream()
            .map(v -> Map.of(
                "field", v.getPropertyPath().toString(),
                "message", v.getMessage()))
            .toList();

    ProblemDetail problem =
        problem(HttpStatus.BAD_REQUEST, "validation-failed", "Validation failed");
    problem.setTitle("Validation failed");
    problem.setProperty("errors", errors);
    return problem;
  }

  @ExceptionHandler(HttpMessageNotReadableException.class)
  ProblemDetail onMalformedBody(HttpMessageNotReadableException exception) {
    // The parser message can quote the request body back, so it is logged, not returned.
    log.debug("malformed request body", exception);
    return problem(
        HttpStatus.BAD_REQUEST, "malformed-request", "The request body is not valid JSON.");
  }

  @ExceptionHandler({
    MissingServletRequestParameterException.class,
    MethodArgumentTypeMismatchException.class
  })
  ProblemDetail onBadParameter(Exception exception) {
    String message =
        exception instanceof MissingServletRequestParameterException missing
            ? "Required parameter '%s' is missing.".formatted(missing.getParameterName())
            : "A request parameter has the wrong type.";
    return problem(HttpStatus.BAD_REQUEST, "invalid-parameter", message);
  }

  @ExceptionHandler(OptimisticLockingFailureException.class)
  ProblemDetail onStaleWrite(OptimisticLockingFailureException exception) {
    log.info("optimistic lock failure", exception);
    return problem(
        HttpStatus.PRECONDITION_FAILED,
        "stale-version",
        "The resource changed since you read it. Re-read it and retry.");
  }

  @ExceptionHandler(Exception.class)
  ProblemDetail onUnexpected(Exception exception) {
    // Log the cause; return nothing about it. The correlation id is how support ties the
    // caller's 500 back to this line.
    log.error("unhandled exception", exception);
    return problem(
        HttpStatus.INTERNAL_SERVER_ERROR,
        "internal-error",
        "The request could not be completed. Quote the correlationId when reporting this.");
  }

  private ProblemDetail problem(HttpStatus status, String code, String detail) {
    ProblemDetail problem = ProblemDetail.forStatusAndDetail(status, detail);
    problem.setType(URI.create(PROBLEM_BASE + code));
    problem.setProperty("correlationId", MDC.get(MDC_CORRELATION_ID));
    return problem;
  }
}
```

## The correlation id filter

```java
@Component
@Order(1)
public class CorrelationIdFilter extends OncePerRequestFilter {

  private static final String HEADER = "X-Correlation-ID";
  private static final String MDC_KEY = "correlationId";

  @Override
  protected void doFilterInternal(
      HttpServletRequest request, HttpServletResponse response, FilterChain chain)
      throws ServletException, IOException {

    String correlationId = request.getHeader(HEADER);
    if (correlationId == null || correlationId.isBlank() || correlationId.length() > 128) {
      // The header is caller-controlled and ends up in log lines, so an absent or
      // implausible value is replaced rather than propagated.
      correlationId = UUID.randomUUID().toString();
    }

    MDC.put(MDC_KEY, correlationId);
    response.setHeader(HEADER, correlationId);
    try {
      chain.doFilter(request, response);
    } finally {
      MDC.remove(MDC_KEY);
    }
  }
}
```

The length cap and blank check matter: without them a caller can inject arbitrary text into
your log lines, including newlines.

## Configuration that affects what leaks

```yaml
server:
  error:
    include-stacktrace: never
    include-message: never   # the advice decides what to say, not the framework
spring:
  mvc:
    problemdetails:
      enabled: true          # framework-raised errors use RFC 9457 too
  jackson:
    deserialization:
      fail-on-unknown-properties: true   # an unexpected field is a contract mismatch
```

`fail-on-unknown-properties: true` is a deliberate choice: it turns a client sending a field
you do not know about into a 400 rather than a silent drop. Prefer it for internal APIs. For a
public API with many consumers, lenient parsing may be the better trade — decide it, do not
inherit it.

## What to assert in tests

Each error path deserves a slice test (`@WebMvcTest`) checking:

- the status code
- `Content-Type: application/problem+json`
- `$.type`, `$.title`, `$.status` present
- field-level `$.errors[0].field` for validation failures
- that the service was **not** called when the request failed validation
  (`verifyNoInteractions`) — this catches a controller doing work before validation

And one test proving the 500 path returns nothing internal: assert that the response body
does **not** contain the exception's message. That is the assertion that stops a future
refactor from reintroducing the leak.
