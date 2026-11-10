"""Typed artifacts exchanged between pipeline stages.

Stages communicate only through these models, serialised to JSON. No stage reaches into
another's internals, which is what lets each one be tested and re-run in isolation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Severity = Literal["error", "warn", "info"]


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --------------------------------------------------------------------------------------
# S1 Validate
# --------------------------------------------------------------------------------------


class Location(Artifact):
    file: str
    line: int | None = None
    column: int | None = None


class Finding(Artifact):
    """One validation finding.

    ``remediation`` and ``auto_fixable`` are not decoration: they are what lets the
    yaml-validator agent repair a spec rather than merely describe what is wrong with it.
    """

    id: str
    rule: str
    severity: Severity
    message: str
    pointer: str
    location: Location | None = None
    remediation: str | None = None
    auto_fixable: bool = Field(default=False, alias="autoFixable")


class ValidationSummary(Artifact):
    errors: int = 0
    warnings: int = 0
    infos: int = 0
    redactions: int = 0


class Gate(Artifact):
    passed: bool
    failed_on: str | None = Field(default=None, alias="failedOn")


class ValidationReport(Artifact):
    spec_hash: str = Field(alias="specHash")
    spec_version: str = Field(alias="specVersion")
    detected_version: str = Field(alias="detectedVersion")
    summary: ValidationSummary
    findings: list[Finding] = Field(default_factory=list)
    gate: Gate

    @staticmethod
    def build(
        *,
        spec_hash: str,
        detected_version: str,
        canonical_version: str,
        findings: list[Finding],
        fail_on: Severity = "error",
        redactions: int = 0,
    ) -> ValidationReport:
        errors = sum(1 for f in findings if f.severity == "error")
        warnings = sum(1 for f in findings if f.severity == "warn")
        infos = sum(1 for f in findings if f.severity == "info")
        blocking = errors if fail_on == "error" else errors + warnings
        return ValidationReport(
            spec_hash=spec_hash,
            spec_version=canonical_version,
            detected_version=detected_version,
            summary=ValidationSummary(
                errors=errors, warnings=warnings, infos=infos, redactions=redactions
            ),
            findings=findings,
            gate=Gate(passed=blocking == 0, failed_on=fail_on if blocking else None),
        )


# --------------------------------------------------------------------------------------
# S2 Normalize
# --------------------------------------------------------------------------------------


class DowngradeEntry(Artifact):
    """One construct affected by a version downgrade.

    ``load_bearing`` is the field that matters: a degradable loss is reported and the run
    continues, while a load-bearing loss changes what the API does and stops the pipeline.
    """

    # Named with a suffix because bare `construct` shadows a Pydantic BaseModel method; the
    # JSON key stays `construct` via the alias.
    construct_name: str = Field(alias="construct")
    pointer: str
    classification: Literal["degradable", "load-bearing"]
    action: str
    impact: str | None = None

    @property
    def load_bearing(self) -> bool:
        return self.classification == "load-bearing"


class DowngradeReport(Artifact):
    source_version: str = Field(alias="sourceVersion")
    target_version: str = Field(alias="targetVersion")
    entries: list[DowngradeEntry] = Field(default_factory=list)

    @property
    def load_bearing_losses(self) -> list[DowngradeEntry]:
        return [e for e in self.entries if e.load_bearing]


class Redaction(Artifact):
    pointer: str
    pattern: str
    excerpt: str


class SanitizationReport(Artifact):
    redactions: list[Redaction] = Field(default_factory=list)


class NormalizationResult(Artifact):
    canonical_path: str = Field(alias="canonicalPath")
    canonical_hash: str = Field(alias="canonicalHash")
    source_version: str = Field(alias="sourceVersion")
    target_version: str = Field(alias="targetVersion")
    normalizations: list[str] = Field(default_factory=list)
    downgrade_report: DowngradeReport | None = Field(default=None, alias="downgradeReport")
    sanitization_report: SanitizationReport | None = Field(
        default=None, alias="sanitizationReport"
    )


# --------------------------------------------------------------------------------------
# S3 Plan
# --------------------------------------------------------------------------------------


class PlannedOperation(Artifact):
    operation_id: str = Field(alias="operationId")
    method: str
    path: str
    pointer: str
    request_type: str | None = Field(default=None, alias="requestType")
    response_type: str | None = Field(default=None, alias="responseType")
    success_status: int = Field(alias="successStatus")
    error_statuses: list[int] = Field(default_factory=list, alias="errorStatuses")
    security: list[str] = Field(default_factory=list)
    # The effective OpenAPI security requirement, structure preserved: the list is OR, each
    # mapping is AND of schemes, each scheme lists required scopes. An empty list means the
    # operation is public; an empty mapping inside it means anonymous access is one option.
    # `security` above is the flattened view kept for display and backwards compatibility.
    security_requirements: list[dict[str, list[str]]] = Field(
        default_factory=list, alias="securityRequirements"
    )
    idempotent: bool = False
    paginated: bool = False
    optimistic_concurrency: bool = Field(default=False, alias="optimisticConcurrency")
    enum_fields: list[str] = Field(default_factory=list, alias="enumFields")
    risk_factors: list[str] = Field(default_factory=list, alias="riskFactors")
    required_tests: list[str] = Field(default_factory=list, alias="requiredTests")

    @property
    def requires_authentication(self) -> bool:
        """True when no alternative in the requirement allows an anonymous caller."""
        return bool(self.security_requirements) and all(
            bool(requirement) for requirement in self.security_requirements
        )

    @property
    def requires_scopes(self) -> bool:
        """True when every alternative demands at least one scope.

        Only then can a token with the wrong scopes be refused, so only then is a 403 case
        meaningful to require.
        """
        return self.requires_authentication and all(
            any(scopes for scopes in requirement.values())
            for requirement in self.security_requirements
        )

    def marker(self, case: str) -> str:
        """The marker a test carries to claim ``case`` for this operation."""
        return f"o2j:{self.operation_id}:{case}"


class PlannedResource(Artifact):
    tag: str
    api_interface: str = Field(alias="apiInterface")
    delegate: str
    service: str
    operations: list[PlannedOperation] = Field(default_factory=list)


class PlanTarget(Artifact):
    java_version: int = Field(alias="javaVersion")
    spring_profile: str = Field(alias="springProfile")
    spring_boot_version: str = Field(alias="springBootVersion")
    build_tool: str = Field(alias="buildTool")
    web_stack: str = Field(alias="webStack")
    java_tier: int = Field(alias="javaTier")


class RiskScore(Artifact):
    value: int
    band: Literal["low", "medium", "high"]
    drivers: list[str] = Field(default_factory=list)


class GenerationPlan(Artifact):
    spec_hash: str = Field(alias="specHash")
    spec_version: str = Field(alias="specVersion")
    original_version: str = Field(alias="originalVersion")
    target: PlanTarget
    base_package: str = Field(alias="basePackage")
    # Path prefix openapi-generator puts on every controller, taken from servers[0].url.
    base_path: str = Field(default="", alias="basePath")
    # Security scheme name -> kind: oauth2, openIdConnect, http:bearer, http:basic, apiKey,
    # mutualTLS. The kind decides how a requirement is enforced.
    security_schemes: dict[str, str] = Field(default_factory=dict, alias="securitySchemes")
    resources: list[PlannedResource] = Field(default_factory=list)
    risk_score: RiskScore = Field(alias="riskScore")

    @property
    def operations(self) -> list[PlannedOperation]:
        return [op for r in self.resources for op in r.operations]


# --------------------------------------------------------------------------------------
# S4 Generate / S7 Gate / S8 Report
# --------------------------------------------------------------------------------------


class GenerationResult(Artifact):
    spec_hash: str = Field(alias="specHash")
    generator_version: str = Field(alias="generatorVersion")
    output_dir: str = Field(alias="outputDir")
    files: list[str] = Field(default_factory=list)
    scaffolded: list[str] = Field(default_factory=list)
    # Files o2j owns and rewrites on every run because they are derived from the spec, like
    # the security rules. Never hand-edited; a spec change must reach them.
    managed: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    command: list[str] = Field(default_factory=list)


class ToolResult(Artifact):
    tool: str
    status: Literal["pass", "fail", "skipped", "unavailable"]
    findings: int = 0
    detail: str | None = None


class QualityReport(Artifact):
    generated_at: str = Field(default_factory=_utc_now, alias="generatedAt")
    tools: list[ToolResult] = Field(default_factory=list)
    gate: Gate

    @staticmethod
    def build(tools: list[ToolResult]) -> QualityReport:
        failed = [t.tool for t in tools if t.status == "fail"]
        return QualityReport(
            tools=tools,
            gate=Gate(passed=not failed, failed_on=", ".join(failed) if failed else None),
        )


class CoverageReport(Artifact):
    line_coverage: float | None = Field(default=None, alias="lineCoverage")
    branch_coverage: float | None = Field(default=None, alias="branchCoverage")
    service_line_coverage: float | None = Field(default=None, alias="serviceLineCoverage")
    mutation_score: float | None = Field(default=None, alias="mutationScore")
    thresholds: dict[str, int] = Field(default_factory=dict)
    gate: Gate


class VerifyReport(Artifact):
    mode: Literal["determinism", "drift", "contract"]
    passed: bool
    detail: str
    differences: list[str] = Field(default_factory=list)


class Manifest(Artifact):
    """``o2j.lock.json`` - the audit record for one generation run.

    ``generated_at`` is excluded from the content hash on purpose: including it would make
    the determinism check fail on its own timestamp.
    """

    schema_version: int = Field(default=1, alias="schemaVersion")
    generated_at: str = Field(default_factory=_utc_now, alias="generatedAt")
    spec: dict[str, Any] = Field(default_factory=dict)
    target: dict[str, Any] = Field(default_factory=dict)
    tools: dict[str, str] = Field(default_factory=dict)
    operations: int = 0
    generated_files: int = Field(default=0, alias="generatedFiles")
    hand_written_files: int = Field(default=0, alias="handWrittenFiles")
    quality: dict[str, Any] = Field(default_factory=dict)
    determinism: dict[str, Any] = Field(default_factory=dict)

    def content_for_hash(self) -> dict[str, Any]:
        payload = self.model_dump(by_alias=True)
        payload.pop("generatedAt", None)
        return payload
