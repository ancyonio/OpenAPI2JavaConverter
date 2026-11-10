"""S4 Generate - the deterministic contract layer, plus scaffolding for hand-written code.

Two different guarantees live here, and keeping them apart is the point of the whole design:

* The contract layer (DTOs, API interfaces, controllers) is produced by openapi-generator into
  ``target/generated-sources``. It is reproducible, never hand-edited, and regenerated on every
  build.
* A few files are *managed*: derived from the spec but outside openapi-generator's reach, like
  the security rules. o2j rewrites them on every run, exactly like generated sources, so a spec
  change cannot leave them stale. They say so in their header and are never hand-edited.
* The hand-written layers are *scaffolded once*. If a file already exists, this stage writes
  ``<name>.o2j-new`` beside it and reports a conflict. It never overwrites human or agent code,
  because that is what makes regenerating over an evolving spec safe.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from o2j import security
from o2j.config import O2JConfig
from o2j.errors import GenerationFailed
from o2j.models import GenerationPlan, GenerationResult
from o2j.template import render
from o2j.workdir import WorkDir

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

# Scaffold templates and where they land, relative to the service root. The delegate and
# service templates are rendered once per resource, so they are handled separately.
SCAFFOLD: tuple[tuple[str, str], ...] = (
    ("pom.mustache", "pom.xml"),
    ("Application.java.mustache", "src/main/java/{pkg_path}/Application.java"),
    (
        "GlobalExceptionHandler.java.mustache",
        "src/main/java/{pkg_path}/error/GlobalExceptionHandler.java",
    ),
    ("CorrelationId.java.mustache", "src/main/java/{pkg_path}/error/CorrelationId.java"),
    ("DomainException.java.mustache", "src/main/java/{pkg_path}/error/DomainException.java"),
    ("NotFoundException.java.mustache", "src/main/java/{pkg_path}/error/NotFoundException.java"),
    ("ConflictException.java.mustache", "src/main/java/{pkg_path}/error/ConflictException.java"),
    (
        "CorrelationIdFilter.java.mustache",
        "src/main/java/{pkg_path}/config/CorrelationIdFilter.java",
    ),
    ("SecurityConfig.java.mustache", "src/main/java/{pkg_path}/config/SecurityConfig.java"),
    (
        "ProblemDetailsSecurityHandler.java.mustache",
        "src/main/java/{pkg_path}/config/ProblemDetailsSecurityHandler.java",
    ),
    ("MapStructConfig.java.mustache", "src/main/java/{pkg_path}/mapper/MapStructConfig.java"),
    ("application.yml.mustache", "src/main/resources/application.yml"),
    (
        "ArchitectureTest.java.mustache",
        "src/test/java/{pkg_path}/architecture/ArchitectureTest.java",
    ),
)

# Spec-derived files o2j owns and rewrites on every run.
MANAGED: tuple[tuple[str, str], ...] = (
    (
        "OpenApiSecurityRules.java.mustache",
        "src/main/java/{pkg_path}/config/OpenApiSecurityRules.java",
    ),
    (
        "OpenApiSecurityContractTest.java.mustache",
        "src/test/java/{pkg_path}/security/OpenApiSecurityContractTest.java",
    ),
)


def generate(
    cfg: O2JConfig,
    plan: GenerationPlan | None = None,
    output_root: str | Path | None = None,
    work: WorkDir | None = None,
    run_generator: bool = True,
) -> GenerationResult:
    """Render the POM and scaffolding, then invoke openapi-generator via Maven."""
    work = work or WorkDir()
    plan = plan or work.read_json("plan", "generation-plan.json", GenerationPlan)
    root = Path(output_root) if output_root else cfg.project_root

    canonical = work.path("normalize", "spec.canonical.yaml", create=False)
    if not canonical.is_file():
        raise GenerationFailed(
            f"canonical spec not found: {canonical}", hint="run `o2j normalize` first"
        )

    # The service build reads the spec from its own tree, so the build is self-contained and
    # does not depend on o2j's working directory still being around.
    spec_target = root / "openapi" / "spec.canonical.yaml"
    spec_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(canonical, spec_target)

    context = _build_context(cfg, plan)
    scaffolded: list[str] = []
    managed: list[str] = []
    conflicts: list[str] = []

    for template_name, destination in MANAGED:
        target = root / destination.format(pkg_path=context["pkg_path"])
        _write_managed(template_name, target, context, managed, root)

    for template_name, destination in SCAFFOLD:
        target = root / destination.format(pkg_path=context["pkg_path"])
        _write_once(template_name, target, context, scaffolded, conflicts, root)

    for resource in plan.resources:
        res_ctx = {**context, **_resource_context(resource, context)}
        _write_once(
            "DelegateImpl.java.mustache",
            root / f"src/main/java/{context['pkg_path']}/api/{resource.delegate}.java",
            res_ctx,
            scaffolded,
            conflicts,
            root,
        )
        _write_once(
            "Mapper.java.mustache",
            root / f"src/main/java/{context['pkg_path']}/mapper/{res_ctx['mapper']}.java",
            res_ctx,
            scaffolded,
            conflicts,
            root,
        )
        _write_once(
            "Service.java.mustache",
            root / f"src/main/java/{context['pkg_path']}/service/{resource.service}.java",
            res_ctx,
            scaffolded,
            conflicts,
            root,
        )

    command: list[str] = []
    files: list[str] = []
    if run_generator:
        command, files = _invoke_generator(cfg, root)

    return GenerationResult(
        spec_hash=plan.spec_hash,
        generator_version=cfg.versions.openapi_generator,
        output_dir=cfg.generation.output_dir,
        files=files,
        scaffolded=scaffolded,
        managed=managed,
        conflicts=conflicts,
        command=command,
    )


def _write_once(
    template_name: str,
    target: Path,
    context: dict[str, Any],
    scaffolded: list[str],
    conflicts: list[str],
    root: Path,
) -> None:
    """Render a template, but never clobber an existing file.

    A conflict produces ``<name>.o2j-new`` and a report entry. Overwriting would silently
    destroy hand-written business logic on a regeneration, which is the failure mode this
    whole stage is designed around.
    """
    template_path = TEMPLATE_DIR / template_name
    if not template_path.is_file():
        raise GenerationFailed(f"missing scaffold template: {template_path}")

    body = render(template_path.read_text(encoding="utf-8"), context)
    target.parent.mkdir(parents=True, exist_ok=True)
    relative = str(target.relative_to(root)).replace("\\", "/")

    if target.exists():
        existing = target.read_text(encoding="utf-8")
        if existing.replace("\r\n", "\n") == body:
            return  # already current; nothing to report
        new_file = target.with_suffix(target.suffix + ".o2j-new")
        new_file.write_text(body, encoding="utf-8", newline="\n")
        conflicts.append(relative)
        return

    target.write_text(body, encoding="utf-8", newline="\n")
    scaffolded.append(relative)


def _write_managed(
    template_name: str,
    target: Path,
    context: dict[str, Any],
    managed: list[str],
    root: Path,
) -> None:
    """Render a spec-derived file and always overwrite it.

    Unlike scaffolds, these must follow the spec: a security rule left over from the previous
    spec version is a vulnerability, not a merge conflict.
    """
    template_path = TEMPLATE_DIR / template_name
    if not template_path.is_file():
        raise GenerationFailed(f"missing managed template: {template_path}")
    body = render(template_path.read_text(encoding="utf-8"), context)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8", newline="\n")
    managed.append(str(target.relative_to(root)).replace("\\", "/"))


def _security_context(plan: GenerationPlan) -> dict[str, Any]:
    rules = security.rules(plan)
    by_id = {op.operation_id: op for op in plan.operations}

    def case(rule: security.SecurityRule) -> dict[str, str]:
        return {
            "operation_id": rule.operation_id,
            "method": rule.method,
            "method_name": security.java_identifier(rule.operation_id),
            "sample_path": security.sample_path(rule.pattern),
        }

    ordered = sorted(rules, key=lambda r: r.operation_id)
    return {
        "uses_jwt": security.uses_jwt(plan),
        "always_public": ", ".join(f'"{p}"' for p in security.ALWAYS_PUBLIC),
        "security_rules": [
            {
                "operation_id": r.operation_id,
                "method": r.method,
                "pattern": r.pattern,
                "expression": r.expression,
                "comment": r.comment,
            }
            for r in rules
        ],
        "unauthenticated_cases": [
            case(r) for r in ordered if by_id[r.operation_id].requires_authentication
        ],
        "wrong_scope_cases": [
            case(r) for r in ordered if by_id[r.operation_id].requires_scopes
        ],
        "public_cases": [case(r) for r in ordered if r.expression == "permitAll()"],
    }


def _build_context(cfg: O2JConfig, plan: GenerationPlan) -> dict[str, Any]:
    boot4 = cfg.target.spring_profile == "spring-boot-4"
    return {
        "group_id": cfg.service.group_id,
        "artifact_id": cfg.service.artifact_id,
        "service_name": cfg.service.name,
        "base_package": cfg.service.base_package,
        "pkg_path": cfg.service.base_package.replace(".", "/"),
        "java_version": cfg.target.java_version,
        "spring_boot_version": cfg.spring_boot_version,
        "use_spring_boot_4": boot4,
        "use_jackson_3": boot4,
        "virtual_threads": cfg.target.java_version >= 21,
        "spec_hash": plan.spec_hash,
        "operation_count": len(plan.operations),
        "versions": cfg.versions.model_dump(),
        "generation": cfg.generation.model_dump(),
        "coverage": cfg.quality.coverage.model_dump(),
        "mutation": cfg.quality.mutation.model_dump(),
        "mutation_target": ",".join(cfg.quality.mutation.target_packages)
        or f"{cfg.service.base_package}.service.*",
        "coverage_excludes": [{"pattern": p} for p in cfg.quality.coverage.exclude],
        **_security_context(plan),
    }


def _resource_context(resource: Any, base: dict[str, Any]) -> dict[str, Any]:
    return {
        "tag": resource.tag,
        "api_interface": resource.api_interface,
        "delegate": resource.delegate,
        "service": resource.service,
        "mapper": resource.api_interface.removesuffix("Api") + "Mapper",
        "operations": [
            {
                "operation_id": op.operation_id,
                "method": op.method,
                "path": op.path,
                "success_status": op.success_status,
                "request_type": op.request_type,
                "response_type": op.response_type,
                "paginated": op.paginated,
                "idempotent": op.idempotent,
                "required_test_count": len(op.required_tests),
            }
            for op in resource.operations
        ],
    }


def _invoke_generator(cfg: O2JConfig, root: Path) -> tuple[list[str], list[str]]:
    """Run openapi-generator through the Maven plugin configured in the rendered POM.

    Maven is used rather than the standalone CLI so the generator version, template overrides
    and config options come from the POM that the service itself builds with. One source of
    truth beats two that can disagree.
    """
    mvn = shutil.which("mvn") or shutil.which("mvn.cmd")
    if mvn is None:
        raise GenerationFailed(
            "Maven not found on PATH, so the contract layer cannot be generated",
            hint="install Maven, or run `o2j generate --no-run-generator` to scaffold only",
        )

    command = [mvn, "-B", "-ntp", "-f", str(root / "pom.xml"), "generate-sources"]
    try:
        completed = subprocess.run(
            command,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GenerationFailed("generation timed out after 900s") from exc

    if completed.returncode != 0:
        tail = "\n".join((completed.stdout or completed.stderr).splitlines()[-40:])
        raise GenerationFailed(
            f"openapi-generator failed (exit {completed.returncode}):\n{tail}"
        )

    output = root / cfg.generation.output_dir
    files = (
        sorted(
            str(p.relative_to(root)).replace("\\", "/")
            for p in output.rglob("*.java")
        )
        if output.is_dir()
        else []
    )
    return command, files
