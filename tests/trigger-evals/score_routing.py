"""Score skill-routing judgments against the ground truth.

Judgments are joined to the ground truth **by query text**, read back out of the prompt the
judges were actually given. Joining by position looks simpler and is a trap: rebuilding the
ground truth re-sorts it, the indices shift under answers that were keyed to the old order, and
the scores come out plausible-looking and wrong. Parsing the prompt makes the join immune to
that.

Reports precision as well as recall, and that is the point. Recall alone rewards a description
that claims everything: a skill with an over-broad description triggers on all of its own
queries *and* on its neighbours', which reads as 100% recall while quietly stealing work. With
six skills that share vocabulary, precision is the metric that catches a description reaching
too far.

The queries are split into a tuning set and a held-out set, with the split seeded so it is
stable across runs. Descriptions are revised against the tuning set only; the held-out score is
the one worth believing, because a description tuned against every query it is later scored on
tells you nothing about the next query.

Run: python -I tests/trigger-evals/score_routing.py <judge.json> [<judge.json> ...]
"""

from __future__ import annotations

import json
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

EVALS = Path(__file__).resolve().parent
ROUTING = EVALS / "_routing.json"
SPLIT_SEED = 20261109
HELD_OUT_FRACTION = 0.4


def load_routing() -> list[dict]:
    return json.loads(ROUTING.read_text(encoding="utf-8"))


def split(routing: list[dict]) -> tuple[set[str], set[str]]:
    """Stratify by expected owner, so neither split loses a skill entirely.

    Keyed by query text, not position, so the split stays stable when the ground truth is
    rebuilt and re-sorted.
    """
    by_owner: dict[str, list[str]] = defaultdict(list)
    for item in routing:
        by_owner[item["expected"]].append(item["query"])

    # Seeded deliberately: the split must be reproducible so scores are comparable
    # across runs. Not security-relevant, hence S311 is not applicable here.
    rng = random.Random(SPLIT_SEED)  # noqa: S311
    tune: set[str] = set()
    held: set[str] = set()
    for owner in sorted(by_owner):
        queries = sorted(by_owner[owner])
        rng.shuffle(queries)
        cut = max(1, round(len(queries) * HELD_OUT_FRACTION))
        held.update(queries[:cut])
        tune.update(queries[cut:])
    return tune, held


PROMPT_QUERY = re.compile(r"^(\d+)\.\s+(.*)$")


def load_prompt_order(prompt: Path) -> dict[int, str]:
    """Recover the number -> query mapping the judges actually saw."""
    order: dict[int, str] = {}
    in_requests = False
    for line in prompt.read_text(encoding="utf-8").splitlines():
        if line.startswith("## Requests"):
            in_requests = True
            continue
        if in_requests and line.startswith("## "):
            break
        if not in_requests:
            continue
        match = PROMPT_QUERY.match(line.strip())
        if match:
            order[int(match.group(1))] = match.group(2).strip()
    return order


def load_judgments(paths: list[Path], order: dict[int, str]) -> dict[str, list[str]]:
    """query text -> one answer per judge."""
    answers: dict[str, list[str]] = defaultdict(list)
    for path in paths:
        for entry in json.loads(path.read_text(encoding="utf-8")):
            number = int(entry["n"])
            query = order.get(number)
            if query is None:
                raise SystemExit(f"{path.name}: answer {number} is not in the prompt")
            answers[query].append(str(entry["skill"]).strip())
    return answers


def majority(values: list[str]) -> str:
    return Counter(values).most_common(1)[0][0]


def report(
    routing: list[dict], answers: dict[str, list[str]], selected: set[str], label: str
) -> float:
    skills = sorted({item["expected"] for item in routing} - {"none"})

    # Counted on majority vote, which is what a single real invocation approximates.
    true_positive: Counter[str] = Counter()
    false_positive: Counter[str] = Counter()
    false_negative: Counter[str] = Counter()
    stolen: Counter[tuple[str, str]] = Counter()

    correct = 0
    total = 0
    unstable = 0

    expected_by_query = {item["query"]: item["expected"] for item in routing}

    for query in sorted(selected):
        expected = expected_by_query[query]
        votes = answers.get(query)
        if not votes:
            continue
        total += 1
        picked = majority(votes)
        if len(set(votes)) > 1:
            unstable += 1

        if picked == expected:
            correct += 1
            if expected != "none":
                true_positive[expected] += 1
        else:
            if picked != "none":
                false_positive[picked] += 1
            if expected != "none":
                false_negative[expected] += 1
                stolen[(expected, picked)] += 1

    print(f"\n{'=' * 72}")
    print(f"{label}  —  {correct}/{total} correct ({correct / total * 100:.1f}%)")
    print(f"{'=' * 72}")
    print(f"unstable (judges disagreed): {unstable}/{total}")
    print()
    print(f"{'skill':34} {'recall':>8} {'precision':>10}  {'TP':>3} {'FP':>3} {'FN':>3}")
    print("-" * 72)

    for skill in skills:
        tp = true_positive[skill]
        fp = false_positive[skill]
        fn = false_negative[skill]
        recall = tp / (tp + fn) * 100 if (tp + fn) else float("nan")
        precision = tp / (tp + fp) * 100 if (tp + fp) else float("nan")
        print(
            f"{skill:34} {recall:7.0f}% {precision:9.0f}%  {tp:3} {fp:3} {fn:3}"
        )

    none_expected = [q for q in selected if expected_by_query[q] == "none"]
    none_right = sum(
        1 for q in none_expected if answers.get(q) and majority(answers[q]) == "none"
    )
    if none_expected:
        print(
            f"\n{'(correctly declined)':34} {none_right / len(none_expected) * 100:7.0f}%"
            f"           {none_right:3} of {len(none_expected)}"
        )

    if stolen:
        print("\nmis-routing (expected -> picked):")
        for (expected, picked), count in stolen.most_common():
            print(f"  {expected:32} -> {picked:32} x{count}")

    return correct / total * 100 if total else 0.0


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit("usage: score_routing.py <judge-prompt.md> <judge.json> [...]")
    prompt = Path(sys.argv[1])
    paths = [Path(p) for p in sys.argv[2:]]
    missing = [p for p in paths if not p.is_file()]
    if missing:
        raise SystemExit(f"missing judgment files: {missing}")

    routing = load_routing()
    order = load_prompt_order(prompt)
    answers = load_judgments(paths, order)
    tune, held = split(routing)

    unanswered = {item["query"] for item in routing} - set(answers)
    if unanswered:
        print(f"WARNING: {len(unanswered)} ground-truth query/queries had no judgment")
    unknown = set(answers) - {item["query"] for item in routing}
    if unknown:
        print(f"WARNING: {len(unknown)} judged query/queries are not in the ground truth")

    print(f"judges: {len(paths)}  queries: {len(routing)}")
    print(f"tuning set: {len(tune)}   held-out set: {len(held)}")

    tune_score = report(routing, answers, tune, "TUNING SET")
    held_score = report(routing, answers, held, "HELD-OUT SET")

    print(f"\n{'=' * 72}")
    print(f"tuning {tune_score:.1f}%   held-out {held_score:.1f}%")
    print("Believe the held-out number.")


if __name__ == "__main__":
    main()
