"""S7 Gate - determinism, drift and contract conformance.

These three checks are what make "regenerate from the spec" a safe operation rather than a
hopeful one:

* **determinism** - the same input must always produce the same bytes. Two complete runs
  (normalize, plan, generate) into separate trees are diffed file by file. Without this the
  generated tree cannot be trusted as build output and every regeneration is a diff review.
* **drift** - the code on disk must match the spec hash recorded in ``o2j.lock.json``.
* **contract** - every ``requiredTests`` entry in the plan must be claimed, for its own
  operation, by a test method that ran and passed (see ``o2j/testmarkers.py``).
* **conformance** - every HTTP exchange recorded during the test run must match the spec:
  declared status, declared content type, schema-valid body (see ``o2j/conformance.py``).
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from o2j import conformance
from o2j.config import O2JConfig
from o2j.models import GenerationPlan, VerifyReport
from o2j.specdoc import load_spec
from o2j.stages.generate import generate
from o2j.stages.normalize import normalize
from o2j.stages.plan import plan as plan_stage
from o2j.testmarkers import read_results, scan
from o2j.workdir import WorkDir

# Tests declare which required case they cover with a marker comment on the test method:
#   // o2j:createPet:slice:201-location-header
# A marker is deliberate and greppable; inferring coverage from method names would guess.
REPORT_DIRS = ("target/surefire-reports", "target/failsafe-reports")


def verify(
    cfg: O2JConfig,
    mode: str = "determinism",
    work: WorkDir | None = None,
    root: str | Path | None = None,
    require_results: bool = True,
    with_generator: bool = False,
) -> VerifyReport:
    """Run one verification. ``with_generator`` (determinism only) also runs openapi-generator
    through Maven in each of the two trees, so the generated DTOs and interfaces are compared
    as well as the files o2j renders itself."""
    work = work or WorkDir()
    project = Path(root) if root else cfg.service_dir

    if mode == "determinism":
        return _verify_determinism(cfg, work, with_generator)
    if mode == "drift":
        return _verify_drift(cfg, work, project)
    if mode == "contract":
        return _verify_contract(cfg, work, project, require_results)
    if mode == "conformance":
        return _verify_conformance(work, project)
    raise ValueError(
        f"unknown verify mode {mode!r}; expected determinism, drift, contract or conformance"
    )


def _verify_determinism(
    cfg: O2JConfig, work: WorkDir, with_generator: bool = False
) -> VerifyReport:
    """Run the whole chain twice into separate trees and compare them byte for byte.

    Each run normalizes, plans and generates from scratch: the canonical spec, the POM, every
    managed and scaffolded file, the derived contract tests and - with ``with_generator`` -
    the DTOs and API interfaces openapi-generator writes. Comparing only the canonical spec
    would miss a template that iterates a set, or a timestamp in a rendered file.
    """
    source = work.path("ingest", "spec.raw.yaml", create=False)
    if not source.is_file():
        source = cfg.default_spec()
    if not source.is_file():
        return VerifyReport(
            mode="determinism", passed=False, detail=f"no spec to verify at {source}"
        )

    method = "double-generate-diff" + ("+openapi-generator" if with_generator else "")
    trees: list[dict[str, bytes]] = []
    canonical_hash = ""
    for _ in range(2):
        with tempfile.TemporaryDirectory(prefix="o2j-determinism-") as tmp:
            scratch = WorkDir(Path(tmp) / "work")
            normalized = normalize(cfg, spec_path=source, work=scratch)
            canonical_hash = normalized.canonical_hash
            planned = plan_stage(cfg, work=scratch)
            scratch.write_json("plan", "generation-plan.json", planned)
            service = Path(tmp) / "service"
            generate(cfg, planned, service, scratch, run_generator=with_generator)
            tree = _tree(service, cfg.generation.output_dir)
            tree["<canonical spec>"] = Path(normalized.canonical_path).read_bytes()
            trees.append(tree)

    first, second = trees
    changed = sorted(p for p in first.keys() | second.keys() if first.get(p) != second.get(p))
    if not changed:
        return VerifyReport(
            mode="determinism",
            passed=True,
            detail=(
                f"two generation runs agree on all {len(first)} file(s) "
                f"({method}; spec {canonical_hash})"
            ),
        )

    differences = [f"differs: {p}" for p in changed[:20]]
    path = changed[0]
    if path in first and path in second:
        differences += _first_differences(
            first[path].decode("utf-8", "replace"), second[path].decode("utf-8", "replace")
        )
    return VerifyReport(
        mode="determinism",
        passed=False,
        detail=(
            f"generation is not reproducible: {len(changed)} file(s) differ ({method}). "
            "Usual causes: a generation timestamp, unordered map iteration, or an ordinal "
            "inline-schema name."
        ),
        differences=differences,
    )


def _tree(root: Path, generated_dir: str) -> dict[str, bytes]:
    """Every file under ``root``, keyed by POSIX relative path.

    Of ``target/`` only the generator's output is kept: the rest is Maven bookkeeping, not
    generated code.
    """
    generated = (root / generated_dir).resolve()
    out: dict[str, bytes] = {}
    for file in sorted(root.rglob("*")):
        if not file.is_file():
            continue
        relative = file.relative_to(root)
        if relative.parts[0] == "target" and generated not in file.resolve().parents:
            continue
        out[relative.as_posix()] = file.read_bytes()
    return out


def _first_differences(left: str, right: str, limit: int = 10) -> list[str]:
    out: list[str] = []
    left_lines = left.splitlines()
    right_lines = right.splitlines()
    for i, (a, b) in enumerate(zip(left_lines, right_lines, strict=False), start=1):
        if a != b:
            out.append(f"line {i}: {a!r} != {b!r}")
            if len(out) >= limit:
                return out
    if len(left_lines) != len(right_lines):
        out.append(f"line count {len(left_lines)} != {len(right_lines)}")
    return out


def _verify_drift(cfg: O2JConfig, work: WorkDir, project: Path) -> VerifyReport:
    """Compare the recorded spec hash with the spec the code would be generated from now."""
    lock = project / "o2j.lock.json"
    if not lock.is_file():
        return VerifyReport(
            mode="drift",
            passed=False,
            detail="no o2j.lock.json; run `o2j report` after generating",
        )

    try:
        recorded = json.loads(lock.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return VerifyReport(mode="drift", passed=False, detail=f"o2j.lock.json unreadable: {exc}")

    recorded_hash = (recorded.get("spec") or {}).get("canonicalHash")
    canonical = work.path("normalize", "spec.canonical.yaml", create=False)
    if not canonical.is_file():
        canonical = project / "openapi" / "spec.canonical.yaml"
    if not canonical.is_file():
        return VerifyReport(
            mode="drift", passed=False, detail="no canonical spec; run `o2j normalize`"
        )

    current = "sha256:" + hashlib.sha256(
        canonical.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8")
    ).hexdigest()

    if recorded_hash == current:
        return VerifyReport(mode="drift", passed=True, detail=f"code matches spec {current}")

    return VerifyReport(
        mode="drift",
        passed=False,
        detail=(
            f"the spec has changed since this code was generated "
            f"(recorded {recorded_hash}, current {current}). Regenerate and review the delta."
        ),
    )


def _conformance(
    work: WorkDir, project: Path, plan: GenerationPlan
) -> conformance.ConformanceResult:
    spec = _canonical_spec(work, project)
    if spec is None:
        return conformance.ConformanceResult(found=False)
    return conformance.check(project, spec, plan.base_path)


def _canonical_spec(work: WorkDir, project: Path) -> dict[str, Any] | None:
    for candidate in (
        work.path("normalize", "spec.canonical.yaml", create=False),
        project / "openapi" / "spec.canonical.yaml",
    ):
        if candidate.is_file():
            loaded: dict[str, Any] = json.loads(json.dumps(load_spec(candidate)))
            return loaded
    return None


STUB_ONLY = "only stubbed responses recorded"


def _schema_problem(result: conformance.ConformanceResult, operation_id: str) -> str | None:
    if not result.found:
        return "no recorded exchanges (target/o2j-conformance); the recorder did not run"
    stats = result.by_operation.get(operation_id)
    if stats is not None and stats.violations:
        return f"recorded responses violate the spec: {stats.violations[0]}"
    if stats is None or stats.conforming_success == 0:
        if stats is not None and stats.stubbed_success:
            return (
                f"{STUB_ONLY}: no 2xx from the real implementation was checked against the "
                "spec; add a test that drives the actual delegate and service"
            )
        return "no recorded 2xx response conforms to the spec"
    return None


def _verify_conformance(work: WorkDir, project: Path) -> VerifyReport:
    """Every recorded exchange that belongs to a spec operation must match the spec."""
    try:
        plan = work.read_json("plan", "generation-plan.json", GenerationPlan)
    except Exception:
        return VerifyReport(
            mode="conformance", passed=False, detail="no generation plan; run `o2j plan` first"
        )
    result = _conformance(work, project, plan)
    if not result.found:
        return VerifyReport(
            mode="conformance",
            passed=False,
            detail=(
                "no recorded exchanges under target/o2j-conformance. Run `mvn verify` first; "
                "the generated O2jConformanceRecorder writes them during the tests."
            ),
        )
    untested = sorted(
        op.operation_id for op in plan.operations if op.operation_id not in result.by_operation
    )
    # A stub's 2xx proves the contract layer, not the service; the operation needs a real one.
    stub_only = sorted(
        op_id
        for op_id, stats in result.by_operation.items()
        if stats.conforming_success == 0 and stats.stubbed_success > 0
    )
    violations = result.violations
    summary = (
        f"{result.checked} exchange(s) checked against the spec, {result.ignored} outside it; "
        f"{len(violations)} violation(s)"
    )
    if untested:
        summary += f"; no exchange recorded for {', '.join(untested)}"
    if stub_only:
        summary += f"; {STUB_ONLY} for {', '.join(stub_only)}"
    return VerifyReport(
        mode="conformance",
        passed=not violations and not untested and not stub_only,
        detail=summary,
        differences=(
            violations
            + [f"{o}: no exchange recorded" for o in untested]
            + [f"{o}: {STUB_ONLY}" for o in stub_only]
        )[:50],
    )


def _verify_contract(
    cfg: O2JConfig, work: WorkDir, project: Path, require_results: bool = True
) -> VerifyReport:
    """Check that every required case is claimed, for its operation, by a passing test.

    This is what stops `requiredTests` being advisory. Three things must hold for a case:
    a marker names it *with its operation*, the marker sits on a test method, and (unless
    ``require_results`` is off) that test ran and passed. Without the operation in the marker,
    one ``happy-path`` comment satisfied every operation; without the test-method and result
    checks, a file of comments satisfied everything.
    """
    try:
        plan = work.read_json("plan", "generation-plan.json", GenerationPlan)
    except Exception:
        return VerifyReport(
            mode="contract", passed=False, detail="no generation plan; run `o2j plan` first"
        )

    markers = scan(project / "src" / "test" / "java")
    report_dirs = [project / d for d in REPORT_DIRS]
    results = read_results(report_dirs) if require_results else {}
    if require_results and not results:
        return VerifyReport(
            mode="contract",
            passed=False,
            detail=(
                "no test results under target/surefire-reports or target/failsafe-reports. "
                "Run `mvn verify` first, or pass --static to check markers only."
            ),
        )

    by_claim: dict[str, list[str]] = {}
    unattached: list[str] = []
    for marker in markers:
        where = f"{Path(marker.file).name}:{marker.line}"
        if marker.test is None:
            unattached.append(f"o2j:{marker.claim} at {where} is not on a test method")
            continue
        verdict = "ok"
        if require_results:
            outcome = results.get((marker.test.class_name, marker.test.method))
            if outcome is None:
                verdict = f"{marker.test.class_name}#{marker.test.method} did not run"
            elif not outcome.passed:
                verdict = f"{marker.test.class_name}#{marker.test.method} {outcome.status}"
        by_claim.setdefault(marker.claim, []).append(verdict)

    # `contract:response-schema` is about what the service returned, so a passing test is not
    # enough: the recorded responses for the operation must also conform to the spec.
    schema_check = _conformance(work, project, plan) if require_results else None

    missing: list[str] = []
    total = 0
    for op in plan.operations:
        for required in op.required_tests:
            total += 1
            claim = f"{op.operation_id}:{required}"
            verdicts = by_claim.get(claim)
            if not verdicts:
                missing.append(f"{claim}: no test claims it")
            elif "ok" not in verdicts:
                missing.append(f"{claim}: {verdicts[0]}")
            elif required == "contract:response-schema" and schema_check is not None:
                problem = _schema_problem(schema_check, op.operation_id)
                if problem:
                    missing.append(f"{claim}: {problem}")

    if not missing:
        how = "passing tests" if require_results else "test methods (results not checked)"
        return VerifyReport(
            mode="contract",
            passed=True,
            detail=f"all {total} required test cases are claimed by {how}",
            differences=unattached[:50],
        )

    return VerifyReport(
        mode="contract",
        passed=False,
        detail=(
            f"{len(missing)} of {total} required test cases are not covered by a passing test. "
            "Each test method must carry a `// o2j:<operationId>:<case>` marker."
        ),
        differences=(missing + unattached)[:50],
    )
