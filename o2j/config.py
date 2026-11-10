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
    # springdoc 2.x is built for Spring Boot 3, 3.x for Spring Boot 4. Optional so existing
    # o2j.yaml files stay valid; used only by the spring-boot-4 profile.
    springdoc_boot_4: str = "3.0.3"
    owasp_dependency_check: str
    cyclonedx: str
    # Optional so existing o2j.yaml files stay valid.
    spectral_cli: str = "6.11.1"            # `o2j lint` via npx when no spectral binary exists
    sonar_maven_plugin: str = "4.0.0.4121"  # `o2j quality` when quality.sonar.enabled


class OpenApiConfig(StrictModel):
    # A single spec file, or a folder whose *.yaml / *.yml files are each converted into a
    # service of their own by `o2j run` (see resolve_specs).
    input: str = "openapi"
    canonical_version: str = "3.1.2"
    allow_remote_refs: bool = False
    remote_ref_allowlist: list[str] = Field(default_factory=list)
    max_inline_schemas: int = 0
    max_allof_depth: int = 3
    max_document_bytes: int = 10 * 1024 * 1024


class ValidationConfig(StrictModel):
    # Used by `o2j lint` (Spectral or vacuum) and by editors via .spectral.yaml. Relative to
    # o2j.yaml; when absent there, the ruleset packaged with o2j is used.
    spectral_ruleset: str = "o2j/rules/spectral/house-api.yaml"
    fail_on: Literal["error", "warn"] = "error"
    # The previously released spec(s) to check for breaking changes (O2J-V-011): a file, or a
    # folder holding a file of the same name for each spec in `openapi.input`. null = off.
    breaking_change_baseline: str | None = None
    # error | warn | null. null follows `service.profile`: error in production, warn otherwise.
    breaking_change_severity: Literal["error", "warn"] | None = None
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
    # Where generated services are written, relative to o2j.yaml: one subfolder per service,
    # named after `service.artifact_id`. Each subfolder is a self-contained Maven project.
    service_root: str = "generated_Code"

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
    """SonarQube / SonarCloud analysis, run by `o2j quality`. The token comes from $SONAR_TOKEN."""

    enabled: bool = False
    project_key: str = ""
    host_url: str = ""        # empty: $SONAR_HOST_URL, else https://sonarcloud.io
    organization: str = ""    # SonarCloud only


class QualityConfig(StrictModel):
    coverage: CoverageConfig = Field(default_factory=CoverageConfig)
    mutation: MutationConfig = Field(default_factory=MutationConfig)
    static_analysis: StaticAnalysisConfig = Field(default_factory=StaticAnalysisConfig)
    sonar: SonarConfig = Field(default_factory=SonarConfig)


class EmsRouteConfig(StrictModel):
    """One TIBCO EMS request queue served by one spec operation (see o2j/ems.py)."""

    destination: str
    operation: str
    # How the reply is correlated: the request's JMSMessageID (the TIBCO request/reply default)
    # or its JMSCorrelationID (when the requester sets one and waits on a shared reply queue).
    correlation: Literal["message-id", "correlation-id"] = "message-id"
    reply_timeout_ms: int | None = None
    # HTTP parameter name -> source. Sources: property:<name>, jms:JMSMessageID,
    # jms:JMSCorrelationID, json:<pointer into the message body>, literal:<value>.
    # Unlisted parameters default to property:<name with non-identifier chars as _>.
    params: dict[str, str] = Field(default_factory=dict)
    # HTTP status -> the legacy fault code EMS consumers already handle.
    faults: dict[int, str] = Field(default_factory=dict)


class EmsConfig(StrictModel):
    """TIBCO EMS request/reply queues the REST service replaces (optional)."""

    enabled: bool = False
    reply_timeout_ms: int = 5000
    routes: list[EmsRouteConfig] = Field(default_factory=list)


class O2JConfig(StrictModel):
    """The whole of ``o2j.yaml``."""

    version: int = 1
    service: ServiceConfig
    target: TargetConfig = Field(default_factory=TargetConfig)
    versions: VersionsConfig
    openapi: OpenApiConfig = Field(default_factory=OpenApiConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    ems: EmsConfig = Field(default_factory=EmsConfig)
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
        """The directory holding o2j.yaml; spec inputs resolve against it."""
        return self.source_path.parent if self.source_path else Path.cwd()

    @property
    def service_dir(self) -> Path:
        """Where this service's code is written: ``<service_root>/<artifact_id>``."""
        return self.project_root / self.generation.service_root / self.service.artifact_id

    def input_path(self) -> Path:
        """``openapi.input`` resolved against the project root (a file or a folder)."""
        return self.project_root / self.openapi.input

    def baseline_for(
        self, spec: str | Path | None, override: str | Path | None = None
    ) -> Path | None:
        """The previous version of ``spec`` to diff against, or None when there is none.

        ``override`` (the --baseline option) wins over ``validation.breaking_change_baseline``.
        A baseline file is used as is; a baseline folder is searched for a file with the same
        name as the spec, so one folder can serve every spec in ``openapi/``. A spec that is new
        has no baseline, which is not an error.
        """
        setting = override if override is not None else self.validation.breaking_change_baseline
        if not setting:
            return None
        base = Path(setting)
        if not base.is_absolute():
            base = (Path.cwd() if override is not None else self.project_root) / base
        if base.is_file():
            return base
        if base.is_dir():
            name = Path(spec).name if spec is not None else None
            if name is None:
                specs = resolve_specs(self.input_path())
                name = specs[0].name if len(specs) == 1 else None
            if name is not None and (base / name).is_file():
                return base / name
        return None

    def default_spec(self) -> Path:
        """The one spec a single-spec command works on when it is given no path.

        ``openapi.input`` may name a folder; a single-spec command can only use it when the
        folder holds exactly one spec, so it never guesses between several.
        """
        specs = resolve_specs(self.input_path())
        if len(specs) == 1:
            return specs[0]
        if not specs:
            return self.input_path()  # let the caller report "spec not found" on this path
        raise ConfigError(
            f"{self.input_path()} holds {len(specs)} specs; this command needs one",
            hint="pass the spec path explicitly, or use `o2j run` to convert every spec",
        )


SPEC_SUFFIXES: tuple[str, ...] = (".yaml", ".yml")


def resolve_specs(path: str | Path) -> list[Path]:
    """The spec files at ``path``: the file itself, or every *.yaml / *.yml in a folder.

    Only the folder's top level is read, in sorted order so batch runs are reproducible.
    """
    target = Path(path)
    if target.is_dir():
        return sorted(
            p for p in target.iterdir() if p.is_file() and p.suffix.lower() in SPEC_SUFFIXES
        )
    return [target] if target.is_file() else []


def service_slug(spec: str | Path) -> str:
    """A Maven artifactId / folder name derived from a spec's file name.

    ``Order API.v2.yaml`` -> ``order-api-v2``.
    """
    stem = Path(spec).stem.lower()
    slug = "".join(c if c.isalnum() else "-" for c in stem)
    slug = "-".join(part for part in slug.split("-") if part)
    return slug or "service"


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
