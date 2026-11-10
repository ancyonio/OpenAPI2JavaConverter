"""S7 Gate - coverage and mutation thresholds.

Line coverage alone cannot tell a test that asserts the right outcome from one that asserts
nothing, and an AI-authored suite reaches a high line number very easily. Mutation score can:
PIT changes the production code and checks whether any test notices. That is why the mutation
threshold is blocking and why this module reports both.
"""

from __future__ import annotations

import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from o2j.config import O2JConfig
from o2j.models import CoverageReport, Gate


def coverage(
    cfg: O2JConfig,
    root: str | Path | None = None,
    run_build: bool = True,
) -> CoverageReport:
    """Run the test build if asked, then read and evaluate the reports."""
    project = Path(root) if root else cfg.project_root
    thresholds = {
        "line_min": cfg.quality.coverage.line_min,
        "branch_min": cfg.quality.coverage.branch_min,
        "service_layer_line_min": cfg.quality.coverage.service_layer_line_min,
        "mutation_threshold": cfg.quality.mutation.threshold,
    }

    if run_build:
        _run_build(cfg, project)

    line, branch, service_line = _read_jacoco(project, cfg.service.base_package)
    mutation = _read_pitest(project) if cfg.quality.mutation.enabled else None

    failures: list[str] = []
    if line is None:
        failures.append("no JaCoCo report")
    else:
        if line < thresholds["line_min"]:
            failures.append(f"line {line:.1f}% < {thresholds['line_min']}%")
        if branch is not None and branch < thresholds["branch_min"]:
            failures.append(f"branch {branch:.1f}% < {thresholds['branch_min']}%")
        if (
            service_line is not None
            and service_line < thresholds["service_layer_line_min"]
        ):
            failures.append(
                f"service line {service_line:.1f}% < {thresholds['service_layer_line_min']}%"
            )

    if cfg.quality.mutation.enabled:
        if mutation is None:
            failures.append("no PIT report")
        elif mutation < thresholds["mutation_threshold"]:
            failures.append(
                f"mutation {mutation:.1f}% < {thresholds['mutation_threshold']}% "
                f"(assertions are too weak, not tests too few)"
            )

    return CoverageReport(
        line_coverage=line,
        branch_coverage=branch,
        service_line_coverage=service_line,
        mutation_score=mutation,
        thresholds=thresholds,
        gate=Gate(
            passed=not failures,
            failed_on="; ".join(failures) if failures else None,
        ),
    )


def _run_build(cfg: O2JConfig, project: Path) -> None:
    mvn = shutil.which("mvn") or shutil.which("mvn.cmd")
    if mvn is None or not (project / "pom.xml").is_file():
        return
    goals = ["verify"]
    if cfg.quality.mutation.enabled:
        goals.append("org.pitest:pitest-maven:mutationCoverage")
    subprocess.run(
        [mvn, "-B", "-ntp", "-f", str(project / "pom.xml"), *goals],
        cwd=str(project),
        capture_output=True,
        text=True,
        timeout=3600,
        check=False,
    )


def _read_jacoco(
    project: Path, base_package: str
) -> tuple[float | None, float | None, float | None]:
    """Read bundle line/branch coverage and the service package's line coverage."""
    report = project / "target" / "site" / "jacoco" / "jacoco.xml"
    if not report.is_file():
        return None, None, None

    try:
        tree = ET.parse(report)  # noqa: S314 - local build output, not untrusted input
    except ET.ParseError:
        return None, None, None
    root = tree.getroot()

    def ratio(node: ET.Element, counter_type: str) -> float | None:
        for counter in node.findall("counter"):
            if counter.get("type") == counter_type:
                covered = float(counter.get("covered", 0))
                missed = float(counter.get("missed", 0))
                total = covered + missed
                return (covered / total * 100.0) if total else None
        return None

    line = ratio(root, "LINE")
    branch = ratio(root, "BRANCH")

    service_line: float | None = None
    service_pkg = f"{base_package}.service".replace(".", "/")
    for package in root.findall("package"):
        if package.get("name") == service_pkg:
            service_line = ratio(package, "LINE")
            break

    return line, branch, service_line


def _read_pitest(project: Path) -> float | None:
    """Mutation score = killed mutants / (total - no-coverage-irrelevant)."""
    candidates = [project / "target" / "pit-reports" / "mutations.xml"]
    candidates += sorted((project / "target" / "pit-reports").glob("*/mutations.xml"))
    report = next((c for c in candidates if c.is_file()), None)
    if report is None:
        return None

    try:
        tree = ET.parse(report)  # noqa: S314 - local build output, not untrusted input
    except ET.ParseError:
        return None

    killed = 0
    total = 0
    for mutation in tree.getroot().findall("mutation"):
        status = (mutation.get("status") or "").upper()
        if status in ("NO_COVERAGE", "SURVIVED", "KILLED", "TIMED_OUT", "MEMORY_ERROR"):
            total += 1
            if status in ("KILLED", "TIMED_OUT", "MEMORY_ERROR"):
                killed += 1

    return (killed / total * 100.0) if total else None
