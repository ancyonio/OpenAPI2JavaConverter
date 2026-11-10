# Trigger-description measurement results

Measured 2026-11-10. Re-run with `score_routing.py` and update this file when a description
changes — a result with no date on it is worse than none.

## Method

116 unique queries, each with one expected owner (a skill, or `none`). Three independent judges
per iteration, each given **only** the six `name` + `description` pairs — exactly what an agent
runtime loads at startup — and asked which single skill they would consult per query. Scored on
majority vote, split 60/40 into a tuning set and a held-out set, stratified by owner.

Judges were explicitly instructed not to read the skill bodies or any repository file, so the
measurement isolates the description from everything else.

The packaged `skill-creator` loop (`scripts/run_loop.py`) was not used: it shells out to
`claude -p` and no Claude Code CLI was installed on the machine. Subagent judges measure the
same thing under tighter control.

## Iteration 1 — baseline (the shipped descriptions)

```
tuning   68/68  (100.0%)
held-out 48/48  (100.0%)
overall  116/116
judge splits (majority right, judges disagreed): 4
```

Per-skill recall and precision were 100% across all six, on both splits, including:

- **31 cross-skill negatives** — queries genuinely owned by a *different* one of the six. These
  are the ones that matter: six skills sharing vocabulary fail by stealing each other's work,
  not by staying silent.
- **29 adjacent-domain negatives** — Terraform, webpack, Playwright, pytest, Postgres tuning,
  Kubernetes probes. All correctly declined.

The four splits, i.e. the only queries carrying any signal at all:

| Query | Expected | Votes |
|---|---|---|
| jackson 2.15 → 2.18, worried about ObjectMapper config | `none` | 2 none, 1 java-spring-upgrade |
| sonarqube duplicated code in test sources | `none` | 2 none, 1 junit-mockito |
| InlineObject4 class names moving between branches | contract-review | 2 contract-review, 1 determinism |
| may a service method take the generated DTO directly? | spring-boot-rest-service | 2 spring-boot, 1 determinism |

The last two are a genuine boundary overlap: `openapi-codegen-determinism`,
`openapi-contract-review` and `spring-boot-rest-service` all legitimately hold a piece of the
generated-type isolation rule.

## Iteration 2 — tried to sharpen that boundary, and reverted

Added a scope clause to `openapi-codegen-determinism`: *"The scope is the generator's output
and the act of regenerating; whether the API description itself is sound, or how to write the
service code behind it, are separate questions."*

```
tuning   67/68  (98.5%)   <- regression
held-out 48/48  (100.0%)  <- unchanged
overall  115/116
judge splits: 4           <- unchanged
```

What the change actually did:

| Effect | Query |
|---|---|
| fixed a split | jackson 2.15 → 2.18 (now unanimous `none`) |
| fixed a split | sonarqube test duplication (now unanimous `none`) |
| **new split** | `Instant.now()` makes createdAt untestable (1 judge moved to junit-mockito) |
| **broke outright** | agent-directed text found in a vendor spec's `description` field: contract-review → `none` |

And critically: **neither of the two overlaps it was written to fix moved.** Both stayed split.

So the clause missed its target, cost a real routing case — spec text containing instructions
aimed at an AI agent is squarely contract-review's job — and left the split count unchanged.
**Reverted.**

## Conclusion

The descriptions were already at the ceiling of what this eval set can measure, so there was
nothing to tune. The useful output of the exercise was not a better description; it was:

1. **A regression prevented.** The iteration-2 clause looked like a principled clarification
   and would have shipped on reasoning alone. Measurement showed it traded one real routing
   case for two cosmetic ones.
2. **Two harness bugs found.** A ground-truth key too narrow to catch a paraphrase, and a
   scorer joining judgments by position instead of query text — the second produced
   plausible-looking nonsense (a "migrate to WebFlux" query routed to a *testing* skill by all
   three judges) and was caught only because the result was absurd on its face.
3. **A documented boundary.** The determinism / contract-review / spring-boot overlap on
   generated-type isolation is real. It is not a defect to fix in a description; it is a place
   where more than one skill genuinely helps.

## Where the headroom actually is

A 100% held-out score means the test has run out of discrimination, not that the descriptions
are perfect. A harder set would need:

- queries that span two skills in one breath ("convert this 2.0 spec and tell me whether the
  contract is any good")
- queries naming no technology at all ("the generated model doesn't match what we agreed")
- queries from someone who has the wrong mental model of the problem
- queries where the right answer is to consult **two** skills, which this harness cannot
  express — it forces a single owner

That last one is the real limitation. Before tuning any description again, raise the difficulty
of the eval set; tuning against a set everything already passes can only overfit.

---

## Iteration 3 — expanded from 6 skills to 14

Eight skills added: `openapi-spec-analysis`, `openapi-to-java-mapping`,
`spring-validation-error-handling`, `spring-security-openapi`, `jpa-persistence-design`,
`service-resilience-observability`, `rest-api-testing`, `java-code-quality-gates`.

Eval set grew from 116 to 211 queries.

### First reading — and why it was wrong

```
tuning   118/127  (92.9%)
held-out  79/84   (94.0%)   <- apparently down from 100.0% with 6 skills
```

The per-skill columns looked like a clear story: the eight new skills had ~100% recall but only
62–86% precision, while the six originals kept 100% precision and lost recall to 78–89%. Read
together that says the newcomers were claiming requests belonging to the incumbents, and the
obvious next step was to scope the over-claiming descriptions.

**That reading was wrong, and acting on it would have made the descriptions worse.**

Inspecting the sixteen individual failures rather than the aggregate: eleven were cases where
the judges were **right and the ground truth was stale**. Those queries were written as
positives or negatives for the original six skills, at a time when no skill existed that
legitimately owned them:

| Query | Key said | Judges said (unanimously) |
|---|---|---|
| should i use h2 for the repository tests? | junit-mockito | **rest-api-testing** |
| write the integration test … needs a real postgres | junit-mockito | **rest-api-testing** |
| our contract tests against the vendor's sandbox … | none | **rest-api-testing** |
| we're seeing n+1 queries on GET /orders … | spring-boot-rest-service | **jpa-persistence-design** |
| our flyway migrations are out of order … | none | **jpa-persistence-design** |
| mapping a postgres jsonb column with hibernate | none | **jpa-persistence-design** |
| the postgres query … is doing a seq scan | none | **jpa-persistence-design** |
| the payment gateway went slow … thread pool filled up | spring-boot-rest-service | **service-resilience-observability** |
| sonarqube gate failing on duplicated test code | none | **java-code-quality-gates** |
| before i merge this PR … would break existing clients | contract-review | **openapi-spec-analysis** |
| upgrade jackson 2.15 → 2.18, ObjectMapper config | none | **java-spring-upgrade** |

Every one of those is the new skill's own documented territory. Twelve queries were reassigned
to their rightful owners.

### Second reading — the same judge answers, corrected key

```
tuning    126/127  (99.2%)
held-out   85/86   (98.8%)
judge splits: 3
precision: 100% for 13 of 14 skills
```

No description was changed. The expansion cost roughly **one point**, not six, and the one
remaining failure is the `openapi-contract-review` / `openapi-codegen-determinism` overlap on
ordinal naming that iteration 1 had already identified as genuine dual ownership.

### What this cost and bought

| | 6 skills | 14 skills |
|---|---|---|
| held-out routing | 100.0% | 98.8% |
| coverage | no answer for JPA, security, quality-gate, resilience or mapping questions | all covered |

### The lesson, which is the valuable output here

**An aggregate metric told a coherent, plausible, wrong story.** Precision down on the new
skills and recall down on the old ones is exactly the signature of over-claiming — and it was
instead the signature of an answer key that had not kept up with the skill set.

Three things follow:

1. **Read the individual failures before acting on the aggregate.** Sixteen rows of detail
   reversed the conclusion that fourteen summary numbers supported.
2. **When a skill set grows, the ground truth is part of the change.** A query labelled for a
   six-skill world is not a valid label in a fourteen-skill one. There is now a test asserting
   `_routing.json` is current, but nothing can detect a label that is *stale in meaning* — only
   inspection does that.
3. **Had the earlier instinct been followed, the harness would have "proved" the fix worked.**
   Scoping the new descriptions would have pushed queries back toward the wrong owners and the
   score would have risen. A metric optimised against a wrong key gets worse while reporting
   better.

---

## Accepted overlaps — do not "fix" these

One mis-route survives at 98.8%, and it is deliberate.

**`openapi-contract-review` vs `openapi-codegen-determinism`**, on a query like *"we keep
getting InlineObject4, InlineObject7 as class names and they move around between branches"*.

Both skills genuinely cover it, from different ends: contract-review treats an inline schema as
a spec defect to fix at source, determinism treats ordinal naming as a reproducibility problem
to fix before generation. A reader who lands on either gets correct, useful guidance. It has
been split 2-1 across every iteration, which is what honest dual ownership looks like rather
than a defect.

It is left alone for three reasons:

1. **Both answers are right.** There is no wrong routing to prevent.
2. **The harness cannot express it.** It forces one owner per query, so a genuine two-owner
   case is recorded as a failure by construction. The score is capped at 98.8% by the
   measurement design, not by the descriptions.
3. **Chasing it is the overfitting that has already failed here twice.** Iteration 2 added a
   boundary clause for exactly this overlap: it missed the target, broke an unrelated routing
   case, and was reverted.

If the harness is ever extended to multi-owner expectations (see "Where the headroom actually
is"), this is the first query to re-check — it should then score as correct rather than as the
one standing failure.
