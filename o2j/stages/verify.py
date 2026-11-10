"""S7 Gate - determinism, drift and contract conformance.

These three checks are what make "regenerate from the spec" a safe operation rather than a
hopeful one:

* **determinism** - the same input must always produce the same bytes. Without this the
  generated tree cannot be trusted as build output and every regeneration is a diff review.
* **drift** - the code on disk must match the spec hash recorded in ``o2j.lock.json``.
* **contract** - every ``requiredTests`` entry in the plan must have a corresponding test.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path

from o2j.config import O2JConfig
from o2j.models import GenerationPlan, VerifyReport
from o2j.stages.normalize import normalize
from o2j.workdir import WorkDir

# Tests declare which required case they cover with a marker comment, e.g.
#   // o2j:slice:201-location-header
# A marker is deliberate and greppable; inferring coverage from method names would guess.
MARKER = re.compile(r"o2j:([a-z]+:[A-Za-z0-9\-:_]+)")


def verify(
    cfg: O2JConfig,
    mode: str = "determinism",
    work: WorkDir | None = None,
    root: str | Path | None = None,
) -> VerifyReport:
    work = work or WorkDir()
    project = Path(root) if root else cfg.project_root

    if mode == "determinism":
        return _verify_determinism(cfg, work)
    if mode == "drift":
        return _verify_drift(cfg, work, project)
    if mode == "contract":
        return _verify_contract(cfg, work, project)
    raise ValueError(f"unknown verify mode {mode!r}; expected determinism, drift or contract")


def _verify_determinism(cfg: O2JConfig, work: WorkDir) -> VerifyReport:
    """Normalize twice into separate directories and compare the canonical output."""
    source = work.path("ingest", "spec.raw.yaml", create=False)
    if not source.is_file():
        source = cfg.project_root / cfg.openapi.input
    if not source.is_file():
        return VerifyReport(
            mode="determinism", passed=False, detail=f"no spec to verify at {source}"
        )

    hashes: list[str] = []
    texts: list[str] = []
    for _ in range(2):
        with tempfile.TemporaryDirectory(prefix="o2j-determinism-") as tmp:
            scratch = WorkDir(tmp)
            result = normalize(cfg, spec_path=source, work=scratch)
            hashes.append(result.canonical_hash)
            texts.append(
                Path(result.canonical_path).read_text(encoding="utf-8")
            )

    if hashes[0] == hashes[1]:
        return VerifyReport(
            mode="determinism",
            passed=True,
            detail=f"two normalization runs agree ({hashes[0]})",
        )

    differences = _first_differences(texts[0], texts[1])
    return VerifyReport(
        mode="determinism",
        passed=False,
        detail=(
            "normalization is not reproducible. Usual causes: a generation timestamp, "
            "unordered map iteration, or an ordinal inline-schema name."
        ),
        differences=differences,
    )


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


def _verify_contract(cfg: O2JConfig, work: WorkDir, project: Path) -> VerifyReport:
    """Check that every required test case has a test claiming it.

    This is what stops `requiredTests` being advisory. A missing marker means either the test
    was not written or it was written without declaring what it covers; both need attention.
    """
    try:
        plan = work.read_json("plan", "generation-plan.json", GenerationPlan)
    except Exception:
        return VerifyReport(
            mode="contract", passed=False, detail="no generation plan; run `o2j plan` first"
        )

    test_root = project / "src" / "test" / "java"
    claimed: set[str] = set()
    if test_root.is_dir():
        for path in test_root.rglob("*.java"):
            for match in MARKER.finditer(path.read_text(encoding="utf-8", errors="replace")):
                claimed.add(match.group(1))

    missing: list[str] = []
    total = 0
    for op in plan.operations:
        for required in op.required_tests:
            total += 1
            if required not in claimed:
                missing.append(f"{op.operation_id}: {required}")

    if not missing:
        return VerifyReport(
            mode="contract",
            passed=True,
            detail=f"all {total} required test cases are claimed by a test marker",
        )

    return VerifyReport(
        mode="contract",
        passed=False,
        detail=(
            f"{len(missing)} of {total} required test cases have no test. Each test must carry "
            f"a `// o2j:<case>` marker naming the case it covers."
        ),
        differences=missing[:50],
    )
