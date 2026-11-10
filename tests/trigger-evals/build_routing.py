"""Build the routing ground truth from the per-skill trigger eval sets.

With six overlapping skills, "does skill X trigger?" is the wrong question. The failure that
costs anything is **mis-routing**: a spec-review question answered by the migration skill, or a
test question answered by the Spring skill. Those skills share vocabulary, so a description
that over-claims steals work from its neighbour.

So the ground truth is one expected owner per query:

* a skill's positives -> that skill
* a negative that another of the six genuinely owns -> that other skill (CROSS_SKILL below)
* everything else -> "none"

The CROSS_SKILL mapping is hand-maintained because paraphrases do not dedupe automatically, and
a paraphrase scored "none" would punish a judge for the right answer.

Run: python -I tests/trigger-evals/build_routing.py
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

EVALS = Path(__file__).resolve().parent

# A negative whose real owner is another of the six. Keyed by a distinctive query prefix.
CROSS_SKILL: dict[str, str] = {
    "convert this swagger 2.0 file": "openapi-version-migration",
    "convert our swagger 2.0 file": "openapi-version-migration",
    "convert our swagger 2.0 spec": "openapi-version-migration",
    "convert this swagger 2.0 document": "openapi-version-migration",
    "can we move the spec from openapi 3.0": "openapi-version-migration",
    "we're on spring boot 3.5.16": "java-spring-upgrade",
    "we're on boot 3.5.16": "java-spring-upgrade",
    "upgrade this project from java 17 to 21": "java-spring-upgrade",
    "upgrade this service from java 17 to 21": "java-spring-upgrade",
    "upgrade to java 21": "java-spring-upgrade",
    "plan the move to 4.x": "java-spring-upgrade",
    "regenerating from the same unchanged spec": "openapi-codegen-determinism",
    "regenerating from an unchanged spec": "openapi-codegen-determinism",
    "regenerating produces a different tree": "openapi-codegen-determinism",
    # Keyed loosely on purpose: the same complaint appears across sets with different
    # numbers in it, and a key that is too specific lets a paraphrase fall through to
    # "none", which then scores the right answer as wrong.
    "my orderservicetest has": "junit-mockito-test-design",
    "orderservice.create has 94%": "junit-mockito-test-design",
    "my mutation score is 31%": "junit-mockito-test-design",
    "write the integration test for the order cancellation": "junit-mockito-test-design",
    "the build is non-deterministic because two tests share": "junit-mockito-test-design",
    "review my ordersapidelegateimpl": "spring-boot-rest-service",
    "review this delegate implementation": "spring-boot-rest-service",
    "implement createorder against the generated delegate": "spring-boot-rest-service",
    "review my orderservice": "spring-boot-rest-service",
    "can you review api/openapi.yaml": "openapi-contract-review",
    "review api/openapi.yaml": "openapi-contract-review",
    "can you take a look at api/openapi.yaml": "openapi-contract-review",
    "our spec is fine but the generated ordersapi interface": "openapi-contract-review",
}


def owner_of_negative(query: str) -> str:
    lowered = query.strip().lower()
    for prefix, skill in CROSS_SKILL.items():
        if lowered.startswith(prefix):
            return skill
    return "none"


def main() -> None:
    skills = sorted(p.stem for p in EVALS.glob("*.json") if not p.stem.startswith("_"))

    positives: dict[str, str] = {}
    negatives: dict[str, str] = {}

    for skill in skills:
        for item in json.loads((EVALS / f"{skill}.json").read_text(encoding="utf-8")):
            query = item["query"].strip()
            if item["should_trigger"]:
                previous = positives.get(query)
                if previous and previous != skill:
                    raise SystemExit(
                        f"two skills both claim {query!r}: {previous} and {skill}"
                    )
                positives[query] = skill
            else:
                negatives.setdefault(query, owner_of_negative(query))

    routing = [{"query": q, "expected": s} for q, s in positives.items()]
    routing += [
        {"query": q, "expected": s}
        for q, s in negatives.items()
        if q not in positives  # a positive elsewhere already has its owner
    ]
    routing.sort(key=lambda r: (r["expected"], r["query"]))

    (EVALS / "_routing.json").write_text(
        json.dumps(routing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    counts = Counter(r["expected"] for r in routing)
    print(f"{len(skills)} skills, {len(routing)} unique queries")
    for name, count in sorted(counts.items()):
        print(f"  {name:32} {count}")

    unmapped = sum(1 for q, s in negatives.items() if s == "none")
    print(f"\nnegatives owned by another skill: {len(negatives) - unmapped}")
    print(f"negatives owned by nothing:        {unmapped}")


if __name__ == "__main__":
    main()
