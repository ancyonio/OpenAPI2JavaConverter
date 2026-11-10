"""S7 Gate - static analysis and architecture rules.

Each tool is run through Maven so its version and configuration come from the rendered POM
rather than from whatever happens to be installed. A tool that is switched off in
``o2j.yaml`` reports ``skipped``; a tool that cannot run at all reports ``unavailable``.
Those are different states and are kept distinct on purpose: "we chose not to" and "we could
not tell" must never look alike in a gate report.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from o2j.config import O2JConfig
from o2j.models import QualityReport, ToolResult

# Maven goal per tool. ArchUnit is not a plugin: it runs as an ordinary test, so it is
# verified by the surefire run rather than a goal of its own.
GOALS: dict[str, str] = {
    "spotless": "spotless:check",
    "checkstyle": "checkstyle:check",
    "pmd": "pmd:check",
    "spotbugs": "spotbugs:check",
    "owasp_dependency_check": "dependency-check:check",
}


def quality(
    cfg: O2JConfig,
    tools: list[str] | None = None,
    root: str | Path | None = None,
) -> QualityReport:
    """Run the configured static-analysis gates."""
    project = Path(root) if root else cfg.service_dir
    settings = cfg.quality.static_analysis
    requested = [
        t for t in (tools or [name for name in GOALS if getattr(settings, name, False)])
        if t != "sonar"
    ]
    results: list[ToolResult] = []

    mvn = shutil.which("mvn") or shutil.which("mvn.cmd")

    for name in requested:
        if not getattr(settings, name, False):
            results.append(ToolResult(tool=name, status="skipped", detail="disabled in o2j.yaml"))
            continue
        if name not in GOALS:
            results.append(
                ToolResult(tool=name, status="unavailable", detail="no Maven goal mapped")
            )
            continue
        if mvn is None:
            results.append(ToolResult(tool=name, status="unavailable", detail="Maven not on PATH"))
            continue
        results.append(_run_goal(mvn, project, name, GOALS[name]))

    if settings.archunit:
        results.append(_run_archunit(mvn, project))
    else:
        results.append(ToolResult(tool="archunit", status="skipped", detail="disabled in o2j.yaml"))

    if settings.gitleaks:
        results.append(_run_gitleaks(project))

    if cfg.quality.sonar.enabled and (tools is None or "sonar" in tools):
        results.append(_run_sonar(cfg, mvn, project))

    return QualityReport.build(results)


def _run_sonar(cfg: O2JConfig, mvn: str | None, project: Path) -> ToolResult:
    """SonarQube / SonarCloud analysis, failing on the server's quality gate.

    Analyses the last build (classes and the JaCoCo report under target/), so it runs after
    `mvn verify`; `o2j run` orders its gate stage that way. The token is read by the scanner
    from $SONAR_TOKEN and never placed on the command line, where it would be visible in the
    process list and CI logs.
    """
    sonar = cfg.quality.sonar
    if mvn is None:
        return ToolResult(tool="sonar", status="unavailable", detail="Maven not on PATH")
    if not sonar.project_key:
        return ToolResult(
            tool="sonar", status="unavailable", detail="set quality.sonar.project_key in o2j.yaml"
        )
    if not os.environ.get("SONAR_TOKEN"):
        return ToolResult(
            tool="sonar", status="unavailable", detail="SONAR_TOKEN is not set; analysis skipped"
        )
    if not (project / "pom.xml").is_file():
        return ToolResult(tool="sonar", status="unavailable", detail="no pom.xml")

    host = sonar.host_url or os.environ.get("SONAR_HOST_URL") or "https://sonarcloud.io"
    command = [
        mvn,
        "-B",
        "-ntp",
        "-f",
        str(project / "pom.xml"),
        f"org.sonarsource.scanner.maven:sonar-maven-plugin:{cfg.versions.sonar_maven_plugin}:sonar",
        f"-Dsonar.projectKey={sonar.project_key}",
        f"-Dsonar.host.url={host}",
        "-Dsonar.qualitygate.wait=true",
        "-Dsonar.coverage.jacoco.xmlReportPaths=target/site/jacoco/jacoco.xml",
    ]
    if sonar.organization:
        command.append(f"-Dsonar.organization={sonar.organization}")

    completed = subprocess.run(
        command, cwd=str(project), capture_output=True, text=True, timeout=1800, check=False
    )
    if completed.returncode == 0:
        return ToolResult(tool="sonar", status="pass")
    output = completed.stdout or completed.stderr
    lines = [
        line.strip()
        for line in output.splitlines()
        if "QUALITY GATE" in line.upper() or "[ERROR]" in line
    ]
    return ToolResult(
        tool="sonar",
        status="fail",
        findings=1,
        detail="\n".join(lines[:20]) or f"exit {completed.returncode}",
    )


def _run_goal(mvn: str, project: Path, name: str, goal: str) -> ToolResult:
    if not (project / "pom.xml").is_file():
        return ToolResult(tool=name, status="unavailable", detail="no pom.xml; run `o2j generate`")

    completed = subprocess.run(
        [mvn, "-B", "-ntp", "-f", str(project / "pom.xml"), goal],
        cwd=str(project),
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    if completed.returncode == 0:
        return ToolResult(tool=name, status="pass")

    output = completed.stdout or completed.stderr
    violations = [
        line.strip()
        for line in output.splitlines()
        if "[ERROR]" in line or "[WARN]" in line
    ]
    return ToolResult(
        tool=name,
        status="fail",
        findings=len(violations),
        detail="\n".join(violations[:20]) or f"exit {completed.returncode}",
    )


def _run_archunit(mvn: str | None, project: Path) -> ToolResult:
    if mvn is None:
        return ToolResult(tool="archunit", status="unavailable", detail="Maven not on PATH")
    if not (project / "pom.xml").is_file():
        return ToolResult(tool="archunit", status="unavailable", detail="no pom.xml")

    completed = subprocess.run(
        [
            mvn,
            "-B",
            "-ntp",
            "-f",
            str(project / "pom.xml"),
            "test",
            "-Dtest=ArchitectureTest",
            "-DfailIfNoTests=false",
        ],
        cwd=str(project),
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    if completed.returncode == 0:
        return ToolResult(tool="archunit", status="pass")
    output = completed.stdout or completed.stderr
    failures = [line.strip() for line in output.splitlines() if "Architecture Violation" in line]
    return ToolResult(
        tool="archunit",
        status="fail",
        findings=len(failures) or 1,
        detail="\n".join(failures[:20]) or f"exit {completed.returncode}",
    )


def _run_gitleaks(project: Path) -> ToolResult:
    gitleaks = shutil.which("gitleaks")
    if gitleaks is None:
        return ToolResult(
            tool="gitleaks",
            status="unavailable",
            detail="gitleaks not on PATH; secret scanning did not run",
        )
    completed = subprocess.run(
        [gitleaks, "detect", "--source", str(project), "--no-banner", "--redact"],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if completed.returncode == 0:
        return ToolResult(tool="gitleaks", status="pass")
    return ToolResult(
        tool="gitleaks",
        status="fail",
        findings=1,
        detail="secrets detected; output withheld to avoid echoing them",
    )
