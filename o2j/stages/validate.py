"""S1 Validate - structural, version, semantic and house-standard checks.

Four passes, each gating the next, so a document with a parse error never produces a hundred
downstream findings about a tree that could not be read in the first place.
"""

from __future__ import annotations

from pathlib import Path

from ruamel.yaml.comments import CommentedMap

from o2j.config import JAVA_TIER1, SUPPORTED_OPENAPI_VERSIONS, O2JConfig
from o2j.models import Finding, Location, ValidationReport
from o2j.rules import naming, semantic
from o2j.specdoc import (
    detect_version,
    load_spec,
    sha256_file,
    version_family,
)
from o2j.workdir import WorkDir


def validate(
    cfg: O2JConfig,
    spec_path: str | Path | None = None,
    work: WorkDir | None = None,
) -> ValidationReport:
    """Validate a spec and return the report. Does not raise on findings.

    The caller decides what to do about the gate: the CLI exits 3, while an agent may want to
    read the findings and repair the spec.
    """
    work = work or WorkDir()
    path = (
        Path(spec_path)
        if spec_path is not None
        else work.path("ingest", "spec.raw.yaml", create=False)
    )
    if not path.is_file():
        path = cfg.project_root / cfg.openapi.input

    # Pass 1: structural. load_spec raises on malformed YAML and on duplicate keys.
    doc: CommentedMap = load_spec(path)
    file = path.name
    findings: list[Finding] = []

    # Pass 2: version.
    detected = detect_version(doc)
    findings.extend(_check_version(doc, file, detected))

    # Configuration cross-check: a non-LTS JDK must not be paired with a production profile.
    findings.extend(_check_target_tier(cfg))

    # Passes 3 and 4: semantics, then house standards. Both run even when the other finds
    # errors, because a spec author wants the whole list, not one problem at a time.
    #
    # Swagger 2.0 is the exception. Its document shape differs structurally - `responses.schema`
    # instead of `content`, parameter `type` instead of `schema`, no media types on errors - so
    # running 3.x rules against it reports dozens of findings about constructs that conversion
    # is about to create correctly. For 2.0 input the 3.x passes are deferred to the canonical
    # document, which is what code is generated from and therefore what must meet the standard.
    family = version_family(detected)
    if family == "2.0":
        findings.extend(semantic.check_injection(doc, file))
        findings.append(
            Finding(
                id="O2J-V-002",
                rule="swagger-2.0-deferred-validation",
                severity="info",
                message=(
                    "Swagger 2.0 input: structural checks ran now; semantic and house-standard "
                    "checks run against the canonical document after conversion."
                ),
                pointer="#/swagger",
                location=Location(file=file, line=1, column=1),
                remediation="Run `o2j validate` on spec.canonical.yaml after `o2j normalize`.",
                auto_fixable=False,
            )
        )
    elif family != "unknown":
        findings.extend(semantic.run_all(doc, file, detected, cfg))
        findings.extend(naming.run_all(doc, file, cfg))

    return ValidationReport.build(
        spec_hash=sha256_file(path),
        detected_version=detected,
        canonical_version=cfg.openapi.canonical_version,
        findings=findings,
        fail_on=cfg.validation.fail_on,
    )


def _check_version(doc: CommentedMap, file: str, detected: str) -> list[Finding]:
    """FR-V-02 - the declared version decides which ruleset applies."""
    if detected == "unknown":
        return [
            Finding(
                id="O2J-V-002",
                rule="version-missing",
                severity="error",
                message=(
                    "No `openapi` or `swagger` version field. Without it the applicable "
                    "ruleset cannot be determined."
                ),
                pointer="#",
                location=Location(file=file, line=1, column=1),
                remediation='Add `openapi: "3.1.2"` at the top level.',
                auto_fixable=False,
            )
        ]

    if detected not in SUPPORTED_OPENAPI_VERSIONS:
        family = version_family(detected)
        if family == "unknown":
            # Forward-compatible: an unrecognised future version is a warning, so a new
            # OpenAPI release does not hard-stop every pipeline on the day it lands.
            return [
                Finding(
                    id="O2J-V-002",
                    rule="unknown-version",
                    severity="warn",
                    message=(
                        f"OpenAPI version {detected!r} is not recognised. Validation will "
                        f"apply the newest known ruleset, which may miss version-specific "
                        f"problems."
                    ),
                    pointer="#/openapi",
                    location=Location(file=file, line=1, column=1),
                    remediation=(
                        "Check for an o2j update, or normalize the document to a supported "
                        "version."
                    ),
                    auto_fixable=False,
                )
            ]
        return [
            Finding(
                id="O2J-V-002",
                rule="unknown-patch-version",
                severity="info",
                message=(
                    f"OpenAPI {detected} is not in the pinned list, but the {family} ruleset "
                    f"applies."
                ),
                pointer="#/openapi",
                location=Location(file=file, line=1, column=1),
                auto_fixable=False,
            )
        ]

    return []


def _check_target_tier(cfg: O2JConfig) -> list[Finding]:
    """AC-21 - a non-LTS JDK is not a production target.

    Java 23 is past its update window; 26 and 27 are non-LTS. Shipping production on one
    means no security backstop, which is a decision a human should make explicitly rather
    than inherit from a config default.
    """
    if cfg.service.profile != "production":
        return []
    if cfg.target.java_version in JAVA_TIER1:
        return []

    return [
        Finding(
            id="O2J-V-014",
            rule="JAVA_TIER2_IN_PRODUCTION",
            severity="error",
            message=(
                f"Java {cfg.target.java_version} is not an LTS release, so it receives no "
                f"security updates, but `service.profile` is `production`."
            ),
            pointer="#",
            location=Location(file=str(cfg.source_path or "o2j.yaml")),
            remediation=(
                "Use Java 17, 21 or 25 for production, or set `service.profile: sandbox` to "
                "acknowledge this is not a production target."
            ),
            auto_fixable=False,
        )
    ]


def to_sarif(report: ValidationReport, spec_file: str) -> dict[str, object]:
    """Render the report as SARIF so findings appear in GitHub code scanning (FR-V-12)."""
    level = {"error": "error", "warn": "warning", "info": "note"}
    rules: dict[str, dict[str, object]] = {}
    results: list[dict[str, object]] = []

    for f in report.findings:
        rules.setdefault(
            f.rule,
            {
                "id": f.rule,
                "name": f.rule,
                "shortDescription": {"text": f.rule.replace("-", " ")},
                "properties": {"o2jId": f.id},
            },
        )
        result: dict[str, object] = {
            "ruleId": f.rule,
            "level": level[f.severity],
            "message": {"text": f.message},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": (f.location.file if f.location else spec_file)},
                        "region": {
                            "startLine": (f.location.line if f.location and f.location.line else 1),
                            "startColumn": (
                                f.location.column if f.location and f.location.column else 1
                            ),
                        },
                    }
                }
            ],
            "properties": {"pointer": f.pointer, "autoFixable": f.auto_fixable},
        }
        if f.remediation:
            result["fixes"] = [{"description": {"text": f.remediation}}]
        results.append(result)

    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "o2j",
                        "informationUri": "https://github.com/acme/o2j",
                        "rules": list(rules.values()),
                    }
                },
                "results": results,
            }
        ],
    }
