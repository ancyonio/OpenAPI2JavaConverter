"""Typed loader for ``o2j.yaml``.

Every version number, threshold and target in the factory comes from here. Nothing else in
the codebase may hardcode a version: that is what makes a Java or Spring Boot bump a
one-line configuration change rather than a sweep through templates and workflows.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from ruamel.yaml import YAML

from o2j.errors import ConfigError

# Java support tiers, SPECIFICATION.md Section 4.2. Tier 1 is LTS and fully supported;
# Tier 2 is non-LTS and best-effort, so it must not be paired with a production profile.
JAVA_TIER1: frozenset[int] = frozenset({17, 21, 25})
JAVA_TIER2: frozenset[int] = frozenset({23, 26, 27})

SUPPORTED_OPENAPI_VERSIONS: tuple[str, ...] = (
    "2.0",
    "3.0.0",
    "3.0.1",
    "3.0.2",
    "3.0.3",
    "3.0.4",
    "3.1.0",
    "3.1.1",
    "3.1.2",
    "3.2.0",
)


class StrictModel(BaseModel):
    """Reject unknown keys so a typo in o2j.yaml fails loudly instead of being ignored."""

    model_config = ConfigDict(extra="forbid")


class ServiceConfig(StrictModel):
    name: str
    group_id: str
    artifact_id: str
    base_package: str
    profile: Literal["production", "sandbox"] = "production"

    @field_validator("base_package")
    @classmethod
    def _valid_java_package(cls, v: str) -> str:
        parts = v.split(".")
        if not all(p.isidentifier() and not p[0].isdigit() for p in parts):
            raise ValueError(f"not a valid Java package: {v!r}")
        return v


class TargetConfig(StrictModel):
    java_version: int = 21
    spring_profile: Literal["spring-boot-3", "spring-boot-4"] = "spring-boot-3"
    build_tool: Literal["maven", "gradle"] = "maven"
    web_stack: Literal["mvc", "webflux"] = "mvc"

    @field_validator("java_version")
    @classmethod
    def _known_tier(cls, v: int) -> int:
        # Forward-compatible by design: an unrecognised future JDK is a warning raised
        # later by `o2j doctor`, not a hard stop here, so adding Java 29 needs no code change.
        # Below 17 is a genuine impossibility: it is the Spring Boot 3.x and 4.x floor.
        if v not in JAVA_TIER1 | JAVA_TIER2 and v < 17:
            raise ValueError(f"Java {v} is below the Spring Boot 3.x/4.x floor of 17")
        return v

    @property
    def tier(self) -> int:
        if self.java_version in JAVA_TIER1:
            return 1
        if self.java_version in JAVA_TIER2:
            return 2
        return 3


class VersionsConfig(StrictModel):
    openapi_generator: str
    spring_boot: str
    spring_boot_4: str
    mapstruct: str
    jacoco: str
    pitest: str
    pitest_junit5: str
    spotless: str
    checkstyle: str
    pmd: str
    spotbugs: str
    archunit: str
    testcontainers: str
    springdoc: str
    owasp_dependency_check: str
    cyclonedx: str


class OpenApiConfig(StrictModel):
    input: str = "openapi/spec.raw.yaml"
    canonical_version: str = "3.1.2"
    allow_remote_refs: bool = False
    remote_ref_allowlist: list[str] = Field(default_factory=list)
    max_inline_schemas: int = 0
    max_allof_depth: int = 3
    max_document_bytes: int = 10 * 1024 * 1024


class ValidationConfig(StrictModel):
    spectral_ruleset: str = "o2j/rules/spectral/house-api.yaml"
    fail_on: Literal["error", "warn"] = "error"
    breaking_change_baseline: str | None = None
    require_security: bool = True
    require_pagination_on_collections: bool = True
    require_problem_details: bool = True


class GenerationConfig(StrictModel):
    delegate_pattern: bool = True
    interface_only: bool = False
    use_tags: bool = True
    use_optional: bool = False
    use_bean_validation: bool = True
    perform_bean_validation: bool = True
    open_api_nullable: bool = False
    hide_generation_timestamp: bool = True
    serializable_model: bool = True
    unhandled_exception: bool = True
    documentation_provider: Literal["springdoc", "none", "source"] = "springdoc"
    annotation_library: Literal["swagger2", "none", "swagger1"] = "swagger2"
    template_overrides: str = "o2j/templates"
    output_dir: str = "target/generated-sources/openapi"

    @model_validator(mode="after")
    def _timestamp_must_be_hidden(self) -> GenerationConfig:
        # A generation timestamp in a file header makes output differ between runs, which
        # breaks `o2j verify --determinism`. Refuse the configuration rather than let the
        # determinism gate fail later with a confusing diff.
        if not self.hide_generation_timestamp:
            raise ValueError(
                "hide_generation_timestamp must remain true; a generation timestamp makes "
                "output non-reproducible and fails the determinism gate (FR-G-05)"
            )
        return self


class CoverageConfig(StrictModel):
    line_min: int = 85
    branch_min: int = 80
    service_layer_line_min: int = 90
    exclude: list[str] = Field(default_factory=list)


class MutationConfig(StrictModel):
    enabled: bool = True
    threshold: int = 70
    coverage_threshold: int = 85
    target_packages: list[str] = Field(default_factory=list)


class StaticAnalysisConfig(StrictModel):
    spotless: bool = True
    checkstyle: bool = True
    pmd: bool = True
    spotbugs: bool = True
    archunit: bool = True
    owasp_dependency_check: bool = True
    gitleaks: bool = True


class SonarConfig(StrictModel):
    enabled: bool = False
    project_key: str = ""


class QualityConfig(StrictModel):
    coverage: CoverageConfig = Field(default_factory=CoverageConfig)
    mutation: MutationConfig = Field(default_factory=MutationConfig)
    static_analysis: StaticAnalysisConfig = Field(default_factory=StaticAnalysisConfig)
    sonar: SonarConfig = Field(default_factory=SonarConfig)


class O2JConfig(StrictModel):
    """The whole of ``o2j.yaml``."""

    version: int = 1
    service: ServiceConfig
    target: TargetConfig = Field(default_factory=TargetConfig)
    versions: VersionsConfig
    openapi: OpenApiConfig = Field(default_factory=OpenApiConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    quality: QualityConfig = Field(default_factory=QualityConfig)

    # Set by the loader; not part of the file.
    source_path: Path | None = Field(default=None, exclude=True)

    @property
    def spring_boot_version(self) -> str:
        """The Boot version implied by the selected profile."""
        return (
            self.versions.spring_boot_4
            if self.target.spring_profile == "spring-boot-4"
            else self.versions.spring_boot
        )

    @property
    def project_root(self) -> Path:
        return self.source_path.parent if self.source_path else Path.cwd()


def _yaml() -> YAML:
    """A YAML instance configured for safe, deterministic round-tripping.

    ``typ="safe"`` is deliberate: spec and config files are untrusted input, and the unsafe
    loader can instantiate arbitrary Python objects.
    """
    y = YAML(typ="safe")
    y.default_flow_style = False
    return y


def load_config(path: str | Path | None = None) -> O2JConfig:
    """Load and validate ``o2j.yaml``.

    Resolution order: explicit ``path``, then ``$O2J_CONFIG``, then ``./o2j.yaml``.
    """
    candidate = (
        Path(path)
        if path is not None
        else Path(os.environ.get("O2J_CONFIG") or "o2j.yaml")
    )
    candidate = candidate.expanduser()
    if not candidate.is_file():
        raise ConfigError(
            f"configuration not found: {candidate}",
            hint="run `o2j init` to create one, or pass --config",
        )

    try:
        raw: Any = _yaml().load(candidate.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ConfigError(f"{candidate} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{candidate} must contain a YAML mapping at the top level")

    try:
        cfg = O2JConfig.model_validate(raw)
    except Exception as exc:
        raise ConfigError(f"{candidate} is invalid:\n{exc}") from exc

    object.__setattr__(cfg, "source_path", candidate.resolve())
    return cfg
