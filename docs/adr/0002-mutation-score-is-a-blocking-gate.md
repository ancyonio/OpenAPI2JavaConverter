# ADR 0002 — Mutation score is a blocking gate, not a report

- **Status:** Accepted
- **Date:** 2026-11-09

## Context

The factory requires 85% line and 80% branch coverage on hand-written code. Those thresholds
alone do not deliver what they appear to.

Line coverage measures whether a line executed. It cannot distinguish a test that asserts the
right outcome from one that asserts nothing at all:

```java
@Test
void createOrder_works() {
  orderService.createOrder(command);   // 100% line coverage of the method
}                                      // zero assertions
```

This matters more here than in a hand-written codebase. An AI-authored suite reaches a high
line number very easily, because exercising code is the easy half and asserting the right
behaviour is the hard half. Coverage is therefore the metric most likely to look healthy while
the suite catches nothing — and it is the metric an agent will optimise if it is the only one
being measured.

## Decision

Three measures, all blocking:

1. **Derived required tests.** `o2j plan` computes a `requiredTests` list per operation from
   its shape — a `POST` returning 201 with an `Idempotency-Key` header gets
   `slice:201-location-header` and `unit:idempotent-replay-single-side-effect`; a paginated
   `GET` gets five boundary cases. Each test claims a case with a `// o2j:<case>` marker, and
   `o2j verify --mode contract` fails if any case has no test.
2. **Coverage thresholds**: 85% line, 80% branch, 90% line on the service layer. Generated
   code, configuration and `Application` are excluded — coverage of a generated getter
   measures the generator.
3. **Mutation score >= 70%** on the service layer, via PIT. PIT changes the production code
   and checks whether a test notices, which is exactly the property line coverage cannot see.

No agent may change a threshold, widen an exclusion, or edit a ruleset to pass a gate. This
appears in `.github/copilot-instructions.md` and in every agent prompt that can edit files,
because moving the bar is the cheapest way to turn a red build green.

## Consequences

**Good.** "85% coverage" means something. A high line number with a low mutation score points
at weak assertions, which is actionable in a way that a coverage percentage is not. The
`requiredTests` list also removes the guesswork from test authoring: the agent is told which
cases must exist rather than asked to be thorough.

**Costs.** PIT is slow — it runs the suite once per mutant — so it is scoped to the service
layer rather than the whole tree, and the full run is a CI concern rather than a pre-commit
one. The mutation gate is expected to be the one that fails most often. That is the gate
working, not the gate misconfigured, and the remedy is stronger assertions rather than more
tests.
