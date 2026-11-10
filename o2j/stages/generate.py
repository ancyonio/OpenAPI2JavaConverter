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

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from o2j import contracttests, security
from o2j.config import O2JConfig
from o2j.errors import GenerationFailed
from o2j.models import GenerationPlan, GenerationResult
from o2j.specdoc import load_spec
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
    (
        "O2jDelegateStub.java.mustache",
        "src/test/java/{pkg_path}/contract/O2jDelegateStub.java",
    ),
    (
        "O2jConformanceRecorder.java.mustache",
        "src/test/java/{pkg_path}/contract/O2jConformanceRecorder.java",
    ),
)

# Generated only when o2j.yaml maps TIBCO EMS queues (ems.enabled).
EMS_MANAGED: tuple[tuple[str, str], ...] = (
    ("EmsRoutes.java.mustache", "src/main/java/{pkg_path}/ems/EmsRoutes.java"),
    ("EmsBridge.java.mustache", "src/main/java/{pkg_path}/ems/EmsBridge.java"),
    (
        "EmsBridgeContractTest.java.mustache",
        "src/test/java/{pkg_path}/ems/EmsBridgeContractTest.java",
    ),
)

SECURITY_CASES = {
    "slice:unauthenticated-401": "requires_authentication",
    "slice:wrong-scope-403": "requires_scopes",
}


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
    root = Path(output_root) if output_root else cfg.service_dir

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
    spec = json.loads(json.dumps(load_spec(canonical)))
    derived = contracttests.derive(plan, spec)
    # EMS bridge cases render into EmsBridgeContractTest, but count as claimed per operation.
    ems_claimed: dict[str, set[str]] = {}
    for op_id, bridged in contracttests.derive_ems(plan, spec).items():
        context["ems_cases"].extend(
            {"method_name": c.method_name, "markers": c.markers, "lines": c.lines}
            for c in bridged.cases
        )
        ems_claimed[op_id] = {m.split(":", 2)[2] for c in bridged.cases for m in c.markers}
        derived[op_id].skipped += bridged.skipped
    scaffolded: list[str] = []
    managed: list[str] = []
    conflicts: list[str] = []

    for template_name, destination in MANAGED:
        target = root / destination.format(pkg_path=context["pkg_path"])
        _write_managed(template_name, target, context, managed, root)

    for template_name, destination in SCAFFOLD:
        target = root / destination.format(pkg_path=context["pkg_path"])
        _write_once(template_name, target, context, scaffolded, conflicts, root)

    generated_cases = 0
    pending_cases = 0
    for resource in plan.resources:
        res_ctx = {**context, **_resource_context(resource, context)}
        tests_ctx = _tests_context(resource, derived, ems_claimed)
        res_ctx.update(tests_ctx)
        generated_cases += tests_ctx["generated_case_count"]
        pending_cases += len(tests_ctx["pending_unit"]) + len(tests_ctx["pending_slice"])
        _write_managed(
            "ApiContractTest.java.mustache",
            root / f"src/test/java/{context['pkg_path']}/contract/"
            f"{resource.api_interface}ContractTest.java",
            res_ctx,
            managed,
            root,
        )
        _write_once(
            "ServiceTest.java.mustache",
            root / f"src/test/java/{context['pkg_path']}/service/{resource.service}Test.java",
            res_ctx,
            scaffolded,
            conflicts,
            root,
        )
        _write_once(
            "BehaviourTest.java.mustache",
            root / f"src/test/java/{context['pkg_path']}/api/"
            f"{resource.api_interface}BehaviourTest.java",
            res_ctx,
            scaffolded,
            conflicts,
            root,
        )
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

    if context["ems_enabled"]:
        for template_name, destination in EMS_MANAGED:
            target = root / destination.format(pkg_path=context["pkg_path"])
            _write_managed(template_name, target, context, managed, root)
        _write_once(
            "EmsBridgeCredentials.java.mustache",
            root / f"src/main/java/{context['pkg_path']}/ems/EmsBridgeCredentials.java",
            context,
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
        generated_cases=generated_cases,
        pending_cases=pending_cases,
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


def _tests_context(
    resource: Any,
    derived: dict[str, contracttests.OperationCases],
    ems_claimed: dict[str, set[str]],
) -> dict[str, Any]:
    """Generated contract cases for a resource, and the required cases still left to people."""
    cases: list[dict[str, Any]] = []
    pending_unit: list[dict[str, str]] = []
    pending_slice: list[dict[str, str]] = []
    generated = 0
    for op in resource.operations:
        operation = derived[op.operation_id]
        claimed = {m.split(":", 2)[2] for c in operation.cases for m in c.markers}
        claimed |= {case for case, flag in SECURITY_CASES.items() if getattr(op, flag)}
        claimed |= ems_claimed.get(op.operation_id, set())
        for contract_case in operation.cases:
            cases.append(
                {
                    "method_name": contract_case.method_name,
                    "markers": contract_case.markers,
                    "lines": contract_case.lines,
                }
            )
        generated += len([c for c in op.required_tests if c in claimed])
        for case in op.required_tests:
            if case in claimed:
                continue
            name = f"{op.operation_id}_{case.split(':', 1)[1]}"
            entry = {
                "operation_id": op.operation_id,
                "case": case,
                "method_name": security.java_identifier(name),
            }
            (pending_unit if case.startswith("unit:") else pending_slice).append(entry)
    return {
        "contract_cases": cases,
        "pending_unit": pending_unit,
        "pending_slice": pending_slice,
        "generated_case_count": generated,
    }


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
        "springdoc_version": cfg.versions.springdoc_boot_4 if boot4 else cfg.versions.springdoc,
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
        **_ems_context(plan),
    }


def _ems_context(plan: GenerationPlan) -> dict[str, Any]:
    routes = plan.ems_routes
    by_id = {op.operation_id: op for op in plan.operations}
    scopes = sorted(
        {
            scope
            for route in routes
            for requirement in by_id[route.operation_id].security_requirements
            for values in requirement.values()
            for scope in values
        }
    )
    return {
        "ems_enabled": bool(routes),
        "ems_scopes": " ".join(scopes),
        "ems_delegates": [{"api_interface": r.api_interface} for r in plan.resources],
        "ems_cases": [],
        "ems_routes": [
            {
                "destination": r.destination,
                "operation_id": r.operation_id,
                "method": r.method,
                "path": r.path,
                "correlation": r.correlation.upper().replace("-", "_"),
                "timeout": r.reply_timeout_ms,
                "has_body": "true" if r.has_body else "false",
                "params": ", ".join(
                    f'new Param("{p.name}", "{p.location}", "{p.source}")' for p in r.params
                ),
                "faults": ", ".join(f'Map.entry({s}, "{c}")' for s, c in r.faults.items()),
                "last": i == len(routes) - 1,
            }
            for i, r in enumerate(routes)
        ],
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
