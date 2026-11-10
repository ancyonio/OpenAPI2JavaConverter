---
name: spring-security-openapi
description: >
  Wire a Spring Boot service's security to what its OpenAPI contract declares — securitySchemes
  to an OAuth2 resource server, declared scopes to method-level authorization, JWT validation,
  and getting 401 versus 403 right. Use this whenever you configure or review Spring Security
  on a REST service, whenever an endpoint is unexpectedly open or unexpectedly forbidden,
  whenever you are mapping a spec's declared scopes onto @PreAuthorize, whenever CORS or CSRF
  needs deciding, and whenever a token is rejected and you need to know why. Reach for it
  before writing a SecurityFilterChain, because an authorization gap looks exactly like working
  code and is found by an attacker rather than by a test.
license: Apache-2.0
compatibility: >
  Spring Boot 3.x/4.x with spring-boot-starter-oauth2-resource-server, Java 17+. Written for
  JWT bearer tokens against a contract that declares its schemes; notes where opaque-token
  introspection differs.
---

# Securing a service against its contract

The contract already says who may call what. `components.securitySchemes` names the mechanism
and each operation's `security` names the scopes it requires. That is a specification for the
security configuration, and the job here is to implement it faithfully rather than invent a
parallel policy.

Two failure modes, and they are not symmetrical:

- **Too open** — an endpoint with no requirement, or a scope check broader than the contract.
  Found by whoever finds it first, and that is the expensive case.
- **Too closed** — a legitimate caller gets 403. Found immediately, in testing, by a human who
  complains. Annoying, cheap.

So when the two readings of a contract differ, implement the narrower one. A false 403 gets
reported; a false 200 does not.

## Reading the contract

```yaml
components:
  securitySchemes:
    oauth2:
      type: oauth2
      flows:
        clientCredentials:
          tokenUrl: https://auth.example.com/oauth2/token
          scopes:
            orders:read: Read orders
            orders:write: Create and cancel orders

security:                      # the default for every operation
  - oauth2: [orders:read]

paths:
  /orders:
    post:
      security:                # overrides the default for this operation
        - oauth2: [orders:write]
```

What each form means, and the one that is easy to misread:

| Spec | Meaning |
|---|---|
| no `security`, no global default | **unauthenticated** — almost always a defect |
| no `security`, global default present | inherits the global requirement |
| `security: []` | deliberately public. An explicit statement, and different from silence |
| `security: [{oauth2: [a, b]}]` | needs scheme `oauth2` with **both** `a` and `b` |
| `security: [{oauth2: [a]}, {apiKey: []}]` | **either** entry satisfies it — the list is OR, the map is AND |

That last row catches people out: a list of requirement objects is alternatives, not a
conjunction. Two entries means two acceptable ways in.

## Resource server configuration

```java
@Configuration
@EnableWebSecurity
@EnableMethodSecurity            // required for @PreAuthorize to do anything
class SecurityConfig {

  @Bean
  SecurityFilterChain filterChain(HttpSecurity http) throws Exception {
    return http
        // Stateless API: no session, so no CSRF token to protect. Disabling CSRF is correct
        // here and wrong the moment a cookie-authenticated browser flow is added.
        .csrf(AbstractHttpConfigurer::disable)
        .sessionManagement(s -> s.sessionCreationPolicy(SessionCreationPolicy.STATELESS))
        .authorizeHttpRequests(auth -> auth
            .requestMatchers("/actuator/health/**").permitAll()
            .requestMatchers("/v3/api-docs/**", "/swagger-ui/**").permitAll()
            // Deny by default. An endpoint added later is closed until someone opens it,
            // which is the direction you want the mistake to go.
            .anyRequest().authenticated())
        .oauth2ResourceServer(oauth2 -> oauth2
            .jwt(jwt -> jwt.jwtAuthenticationConverter(scopeConverter())))
        .exceptionHandling(e -> e
            .authenticationEntryPoint(problemDetailEntryPoint())
            .accessDeniedHandler(problemDetailAccessDeniedHandler()))
        .build();
  }
}
```

Four things in there that are load-bearing:

- **`@EnableMethodSecurity`.** Without it `@PreAuthorize` is inert — the annotation is present,
  the code reads as secured, and nothing is enforced. Silent, and the most common authorization
  gap in a Spring service.
- **`anyRequest().authenticated()` last.** Deny by default, so the next endpoint someone adds
  is closed until opened deliberately.
- **A custom entry point and access-denied handler.** The defaults return an empty body; a
  caller gets a bare 401 with nothing to act on. Return `application/problem+json` like every
  other error.
- **CSRF disabled only because the API is stateless and token-authenticated.** Write the reason
  in a comment, because the decision stops being correct the moment a cookie-based flow appears.

## Scopes to authorization

OAuth2 scopes arrive in a `scope` claim as a space-delimited string, and Spring prefixes them
`SCOPE_` by default. Map them explicitly rather than relying on that:

```java
JwtAuthenticationConverter scopeConverter() {
  var authorities = new JwtGrantedAuthoritiesConverter();
  authorities.setAuthorityPrefix("SCOPE_");
  authorities.setAuthoritiesClaimName("scope");   // some issuers use "scp"
  var converter = new JwtAuthenticationConverter();
  converter.setJwtGrantedAuthoritiesConverter(authorities);
  return converter;
}
```

Then each operation carries exactly the scope its contract declares:

```java
@PreAuthorize("hasAuthority('SCOPE_orders:write')")
public ResponseEntity<OrderResponse> createOrder(...) { ... }
```

Put the annotation on the delegate, not the service: authorization is a boundary concern, and a
service method called from a scheduled job has no authentication to check.

Where a scope is not enough — "may read *their own* orders" — that is a business rule, and it
belongs in the service with the subject passed in explicitly. Do not reach for
`SecurityContextHolder` inside a service: it makes the method untestable without a security
context and hides the dependency from its signature.

## 401 versus 403

Conflating these makes a client's retry logic wrong, because the two have different remedies.

| Status | Means | Client should |
|---|---|---|
| **401** | no credentials, or they are invalid or expired | obtain a new token and retry |
| **403** | credentials are valid, but insufficient | not retry; this needs different permissions |

A 403 for an expired token sends a client into a loop that never succeeds. Make sure the entry
point handles *authentication* failures and the access-denied handler handles *authorization*
failures, rather than one catching both.

## JWT validation

Validate, in this order: signature against the issuer's JWKS, `iss`, `aud`, and expiry. A
missing audience check is the subtle one — a token minted for a different service by the same
issuer will otherwise validate perfectly.

```yaml
spring:
  security:
    oauth2:
      resourceserver:
        jwt:
          issuer-uri: https://auth.example.com
          audiences: [orders-service]
```

Prefer `issuer-uri` over a hardcoded `jwk-set-uri`: it discovers the JWKS endpoint and enables
issuer validation at the same time.

## What never appears in a response

No stack traces, no internal class names, no SQL, no upstream URLs, and no token contents —
including in logs. Log the subject and the correlation id; those identify the request without
being credentials.

Equally: never log a bearer token or an `Authorization` header, and cap or sanitise any header
value you log, since a caller controls it and can inject newlines into your log lines.

## Testing it

The contract's declared statuses give you the cases, and the negative ones matter most:

- **401** with no token, and with an expired or wrong-issuer one
- **403** with a valid token carrying the wrong scope — the test that proves the scope check
  exists rather than merely compiles
- **200** with the correct scope
- A test that an endpoint you believe is public really is, and that one you believe is not, is not

`@WithMockUser` does not exercise the resource server. Use `jwt()` from
`spring-security-test`'s request post-processors, or a real token against a test issuer, so the
converter and claim mapping are actually covered.
