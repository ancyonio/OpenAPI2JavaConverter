"""Preflight checks, and drift detection on the pinned versions.

``--check-versions`` exists because the version facts in this repository carry an "as of"
date and go stale. A tool that quietly works from a year-old pin is worse than one that says
it does not know: the migration-advisor agent is told to run this before giving version
advice.
"""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from typing import Literal

from pydantic import BaseModel, Field

from o2j.config import JAVA_TIER1, JAVA_TIER2, O2JConfig

CheckStatus = Literal["ok", "warn", "missing", "unknown"]

MAVEN_CENTRAL = "https://repo1.maven.org/maven2"
TRACKED_ARTIFACTS: dict[str, tuple[str, str]] = {
    "openapi_generator": ("org/openapitools", "openapi-generator-maven-plugin"),
    "spring_boot": ("org/springframework/boot", "spring-boot-starter-parent"),
}


class Check(BaseModel):
    name: str
    status: CheckStatus
    detail: str
    remedy: str | None = None


class VersionDrift(BaseModel):
    key: str
    pinned: str
    latest: str | None = None
    drifted: bool = False
    note: str | None = None


class DoctorReport(BaseModel):
    checks: list[Check] = Field(default_factory=list)
    drift: list[VersionDrift] = Field(default_factory=list)
    ok: bool = True


def doctor(cfg: O2JConfig, check_versions: bool = False) -> DoctorReport:
    checks: list[Check] = [
        _check_python(),
        _check_java(cfg),
        _check_maven(),
        _check_node_spectral(),
        _check_openapi_schema(),
        _check_gitleaks(),
        _check_target_tier(cfg),
        _check_spring_boot_eol(cfg),
    ]
    drift = _check_version_drift(cfg) if check_versions else []

    blocking = [c for c in checks if c.status == "missing"]
    return DoctorReport(checks=checks, drift=drift, ok=not blocking)


def _check_python() -> Check:
    major, minor = sys.version_info[:2]
    if (major, minor) >= (3, 11):
        return Check(
            name="python",
            status="ok",
            detail=f"{platform.python_version()} on {platform.system()}",
        )
    return Check(
        name="python",
        status="missing",
        detail=f"{platform.python_version()} is below the required 3.11",
        remedy="install Python 3.11 or newer",
    )


def _check_java(cfg: O2JConfig) -> Check:
    java = shutil.which("java")
    if java is None:
        return Check(
            name="java",
            status="missing",
            detail="no JDK on PATH",
            remedy=f"install a JDK {cfg.target.java_version} toolchain",
        )
    try:
        completed = subprocess.run(
            [java, "-version"], capture_output=True, text=True, timeout=30, check=False
        )
    except subprocess.TimeoutExpired:
        return Check(name="java", status="unknown", detail="`java -version` timed out")

    output = completed.stderr or completed.stdout
    match = re.search(r'version "(\d+)', output)
    found = int(match.group(1)) if match else None
    if found is None:
        first_line = output.splitlines()[0] if output else ""
        return Check(name="java", status="unknown", detail=first_line)
    if found == cfg.target.java_version:
        return Check(name="java", status="ok", detail=f"JDK {found} matches the target")
    return Check(
        name="java",
        status="warn",
        detail=f"JDK {found} on PATH, but the target is {cfg.target.java_version}",
        remedy=(
            "the build uses Maven toolchains, so this is fine if a matching toolchain is "
            "registered; otherwise install the target JDK"
        ),
    )


def _check_maven() -> Check:
    mvn = shutil.which("mvn") or shutil.which("mvn.cmd")
    if mvn is None:
        return Check(
            name="maven",
            status="missing",
            detail="no Maven on PATH; generation and all gates are unavailable",
            remedy="install Maven 3.9 or newer",
        )
    return Check(name="maven", status="ok", detail=mvn)


def _check_node_spectral() -> Check:
    """Spectral is optional: `o2j validate` implements the house rules in Python.

    It is what `o2j lint` and editors (through .spectral.yaml) use, so say which way it runs.
    """
    if shutil.which("spectral"):
        return Check(name="spectral", status="ok", detail="spectral CLI available for o2j lint")
    if shutil.which("vacuum"):
        return Check(name="spectral", status="ok", detail="vacuum available for o2j lint")
    if shutil.which("npx"):
        return Check(
            name="spectral",
            status="ok",
            detail="npx available; o2j lint runs Spectral through it (pinned version)",
        )
    return Check(
        name="spectral",
        status="warn",
        detail="no spectral, vacuum or npx: `o2j lint` cannot run (o2j validate is unaffected)",
        remedy="install Node.js, or the vacuum binary, to use o2j lint",
    )


def _check_openapi_schema() -> Check:
    """The official OpenAPI JSON Schemas behind validation's schema pass (O2J-V-003)."""
    try:
        import openapi_spec_validator  # noqa: F401
    except ImportError:
        return Check(
            name="openapi-schema",
            status="missing",
            detail="openapi-spec-validator not installed; the OpenAPI schema check cannot run",
            remedy="pip install -e .",
        )
    return Check(
        name="openapi-schema", status="ok", detail="official OpenAPI JSON Schemas available"
    )


def _check_gitleaks() -> Check:
    if shutil.which("gitleaks"):
        return Check(name="gitleaks", status="ok", detail="gitleaks available")
    return Check(
        name="gitleaks",
        status="warn",
        detail="gitleaks not on PATH; the secret-scanning gate will report unavailable",
        remedy="install gitleaks, or disable quality.static_analysis.gitleaks",
    )


def _check_target_tier(cfg: O2JConfig) -> Check:
    version = cfg.target.java_version
    if version in JAVA_TIER1:
        return Check(name="java-tier", status="ok", detail=f"Java {version} is LTS (tier 1)")
    if version in JAVA_TIER2:
        status: CheckStatus = "missing" if cfg.service.profile == "production" else "warn"
        return Check(
            name="java-tier",
            status=status,
            detail=(
                f"Java {version} is non-LTS (tier 2) and receives no security updates"
                + (
                    "; `service.profile` is `production`"
                    if cfg.service.profile == "production"
                    else ""
                )
            ),
            remedy="use Java 17, 21 or 25 for production, or set `service.profile: sandbox`",
        )
    return Check(
        name="java-tier",
        status="warn",
        detail=f"Java {version} is not in the known tier list (tier 3, smoke only)",
        remedy="confirm Spring Boot supports this JDK before relying on it",
    )


def _check_spring_boot_eol(cfg: O2JConfig) -> Check:
    """RISK-01 - surface the Boot 3.x support position on every run.

    Recorded as of 2026-11-09: Spring Boot 3.5 reached OSS end-of-life on 2026-06-30, with
    3.5.16 as the final OSS patch. This is a warning rather than an error because commercial
    support is a legitimate answer, but it should never be a surprise.
    """
    if cfg.target.spring_profile == "spring-boot-4":
        return Check(
            name="spring-boot-support",
            status="ok",
            detail=f"Spring Boot {cfg.versions.spring_boot_4} is a supported line",
        )
    return Check(
        name="spring-boot-support",
        status="warn",
        detail=(
            f"Spring Boot {cfg.versions.spring_boot} is past open-source end-of-life "
            f"(3.5 ended 2026-06-30, final OSS patch 3.5.16), so it receives no further OSS "
            f"security patches"
        ),
        remedy=(
            "confirm commercial support is in place, or switch "
            "`target.spring_profile` to `spring-boot-4` (Java 17 floor is unchanged)"
        ),
    )


def _check_version_drift(cfg: O2JConfig) -> list[VersionDrift]:
    out: list[VersionDrift] = []
    for key, (group_path, artifact) in TRACKED_ARTIFACTS.items():
        pinned = getattr(cfg.versions, key)
        latest = _latest_version(group_path, artifact)
        out.append(
            VersionDrift(
                key=key,
                pinned=pinned,
                latest=latest,
                drifted=bool(latest and latest != pinned),
                note=(
                    None
                    if latest
                    else "could not reach Maven Central; pin not verified"
                ),
            )
        )
    return out


def _latest_version(group_path: str, artifact: str) -> str | None:
    url = f"{MAVEN_CENTRAL}/{group_path}/{artifact}/maven-metadata.xml"
    try:
        with urllib.request.urlopen(url, timeout=15) as response:  # noqa: S310 - fixed https host
            data = response.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    try:
        root = ET.fromstring(data)  # noqa: S314 - Maven Central metadata
    except ET.ParseError:
        return None
    node = root.find("./versioning/release") or root.find("./versioning/latest")
    return node.text if node is not None and node.text else None
