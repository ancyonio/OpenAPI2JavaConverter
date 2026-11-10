---
name: java-code-quality-gates
description: >
  Configure, triage and keep honest the static-analysis gates on a Java build — Spotless,
  Checkstyle, PMD, SpotBugs, ArchUnit, SonarQube, OWASP dependency-check, gitleaks — including
  how to suppress a false positive without hollowing out the gate. Use this whenever a build
  fails on lint, formatting, static analysis, a Sonar quality gate, a dependency CVE or a
  secret scan; whenever you are setting these tools up or tuning their rulesets; whenever
  someone asks to lower a threshold, widen an exclusion or disable a rule to get a build green;
  and whenever a gate has become noisy enough that people have started ignoring it. Reach for
  it before editing any ruleset or threshold, because those edits are how a green build stops
  meaning anything.
license: Apache-2.0
compatibility: >
  Maven or Gradle on Java 17+. Examples are Maven; the Gradle plugin equivalents exist for
  every tool named and the triage reasoning is identical.
---

# Keeping quality gates worth having

A quality gate has exactly one job: to make a class of defect impossible to merge. It fails at
that job in two ways, and the second is far more common than the first.

**It can be absent** — nobody notices the defect. Obvious, and easily fixed.

**It can be present and hollow** — the threshold has been lowered twice, three packages are
excluded, and the ruleset has four rules switched off. The build is green, everyone believes
the code is checked, and nothing is. This is worse than no gate, because it actively
misinforms.

So the central question whenever a gate fails is not "how do I get this green?" It is **"is
this finding real?"** — and if it is, the code changes, not the gate.

## Triage order

Run everything, then work in this order. It is not arbitrary: style findings are numerous and
auto-fixable, so clearing them first makes the remaining list short enough to think about.

1. **Formatting** — Spotless. Run `spotless:apply` and stop discussing it. Formatting should
   never be the subject of a code review comment.
2. **Correctness and security** — SpotBugs (with FindSecBugs), the security-category PMD rules,
   OWASP dependency-check. These are the findings that correspond to real defects.
3. **Design** — ArchUnit, PMD design rules, Checkstyle complexity metrics. Real but less
   urgent; they predict future defects rather than present ones.
4. **Style** — Checkstyle naming and layout. Auto-fixable or trivial.

Triage by *impact*, not by tool. A tool that reports one critical and forty style issues should
not be worked through in file order.

## The suppression rules

Not every finding is real. Static analysis trades false positives for coverage, and a tool with
zero false positives is a tool that misses things. So suppression is legitimate — the question
is how.

**Narrow, local, and justified:**

```java
@SuppressWarnings("PMD.AvoidCatchingGenericException")
// The advice layer is the one place a bare Exception catch is correct: it is the last
// boundary before the response, and an unhandled type here becomes a 500 with no body.
ProblemDetail onUnexpected(Exception exception) { ... }
```

Three properties make that acceptable: it names one rule, it sits on the smallest possible
scope, and the comment says why the tool is wrong *here*.

**What to refuse, and the reason each is worse than it looks:**

| Request | Why it is worse than it looks |
|---|---|
| Lower the coverage threshold | The number stops describing the codebase and starts describing what the codebase happens to achieve. It will be lowered again. |
| Add a package to the exclusions | Invisible afterwards. Nobody reviews an exclusion list, so the exclusion outlives the reason for it. |
| Switch the rule off in the ruleset | Removes the finding everywhere, including the places where it was right. |
| `@SuppressWarnings("all")` on a class | Suppresses rules that did not exist when it was written. |
| Raise `failurePriority` or `failBuildOnCVSS` | Hides a whole severity band at once. |
| Mark a test `@Disabled` with no issue link | Looks like coverage, provides none. |

If you believe a threshold is genuinely wrong — set by someone unfamiliar with the codebase,
or inherited from a template — say so, make the case, and change it in a commit that changes
nothing else so it is reviewable. What is not acceptable is changing it in the same commit as
the code that failed it, which is how the record of the decision disappears.

**A module-wide exclusion needs a written decision.** An ADR, a ticket, something with a date
and a name on it. Then it gets revisited. Without that it is permanent.

## Configuration that avoids self-inflicted noise

Most gate noise is configuration, not the tool.

- **Exclude generated sources from everything.** Coverage of a generated getter measures the
  generator; a PMD finding in generated code cannot be fixed in that file. Point every tool
  away from `target/generated-sources` and equivalents.
- **Pin every tool version.** A plugin minor bump can add rules and turn a green build red on
  an unrelated commit, which teaches people that gate failures are noise.
- **Fail the build, do not warn.** A warning in a 4,000-line log is not a gate. If a finding is
  not worth failing on, it is not worth reporting.
- **Run the fast ones pre-commit, the slow ones in CI.** Spotless and Checkstyle are seconds;
  dependency-check and mutation testing are minutes. Putting the slow ones in a pre-commit hook
  gets the hook disabled.
- **One source of truth for versions and thresholds.** Scattered across modules, they drift,
  and nobody can answer what the standard actually is.

`references/tool-configuration.md` has working configuration for each tool, with the options
that matter and why.

## What the tools cannot see

The gates catch mechanical defects. They do not catch the expensive ones, so a green build is
the floor of a review and not the whole of it. Things to check by hand:

- Whether the code does what the ticket asked.
- Whether an error path returns something a caller can act on.
- Whether a transaction boundary is in the right place — and whether an annotation that looks
  active actually is.
- Whether a timeout exists on every outbound call.
- Whether a test would fail if the production code were wrong.
- Whether a new dependency was a reasonable choice.

## When a gate is genuinely too noisy

Sometimes the honest answer is that a gate is miscalibrated — a ruleset imported wholesale,
firing on a house convention that is deliberate.

The fix is to narrow the rule to match the convention, not to disable it. Most tools support
this: PMD rule properties, Checkstyle module configuration, SpotBugs exclusion filters scoped
to a pattern. A rule narrowed to the real intent keeps working; a rule switched off does not.

And if a gate is firing hundreds of times on an existing codebase, introduce it as
*ratcheting*: fail on new violations only, with the existing count recorded as a baseline that
may go down and never up. That converts an impossible cleanup into a gradual one without ever
reporting the codebase as clean when it is not.

## Reference files

- `references/tool-configuration.md` — working Maven configuration for each tool, the options
  that matter, and the equivalents in Gradle.
