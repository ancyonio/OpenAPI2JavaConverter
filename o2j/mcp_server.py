"""The MCP adapter: the same stages, exposed to GitHub Copilot agents as typed tools.

Without this, an agent asked to validate a spec improvises a shell command, gets the flags
wrong and misreads the output. With it, the agent calls a typed tool and receives structured
JSON, so determinism moves out of the prompt and into the tool.

Mutating tools are marked in their docstrings. Agent prompts grant the read tools freely but
require the orchestrator to own the mutating calls for its own stage.

Run with ``python -I -m o2j.mcp_server``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from o2j import lint as lint_mod
from o2j.config import load_config, resolve_specs
from o2j.errors import O2JError
from o2j.models import GenerationPlan
from o2j.rulebook import explain
from o2j.stages import coverage as coverage_stage
from o2j.stages import generate as generate_stage
from o2j.stages import normalize as normalize_stage
from o2j.stages import plan as plan_stage
from o2j.stages import quality as quality_stage
from o2j.stages import validate as validate_stage
from o2j.stages import verify as verify_stage
from o2j.workdir import WorkDir

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover - import-time guard with an actionable message
    print(
        "the MCP server needs the `mcp` package: pip install 'o2j[mcp]'",
        file=sys.stderr,
    )
    raise SystemExit(2) from None

mcp = FastMCP("o2j")


def _cfg() -> Any:
    return load_config()


def _error(exc: O2JError) -> dict[str, Any]:
    """Return a failure as data.

    An agent can act on a structured error; it cannot act on a traceback.
    """
    return {
        "ok": False,
        "error": exc.message,
        "hint": exc.hint,
        "exitCode": int(exc.exit_code),
    }


@mcp.tool()
def o2j_validate_spec(
    spec_path: str | None = None,
    fail_on: str | None = None,
    baseline: str | None = None,
) -> dict[str, Any]:
    """Validate an OpenAPI spec: syntax, the official OpenAPI schema, semantics, $ref
    resolution, house standards and - against `baseline` or the configured baseline -
    breaking changes.

    Read-only. Returns a ValidationReport whose findings each carry a JSON Pointer, a
    remediation and an `autoFixable` flag.
    """
    try:
        cfg = _cfg()
        if fail_on:
            cfg.validation.fail_on = fail_on
        work = WorkDir()
        result = validate_stage.validate(cfg, spec_path, work, baseline=baseline)
        work.write_json("validate", "validation-report.json", result)
        return {"ok": True, **result.model_dump(by_alias=True)}
    except O2JError as exc:
        return _error(exc)


@mcp.tool()
def o2j_lint_spec(spec_path: str | None = None) -> dict[str, Any]:
    """Advisory second opinion: Spectral (or vacuum) with the house ruleset.

    Read-only. Use this instead of running `npx spectral lint` yourself; it applies the
    configured ruleset and pinned Spectral version. `o2j_validate_spec` remains the gate.
    """
    try:
        cfg = _cfg()
        source = Path(spec_path) if spec_path else cfg.input_path()
        specs = resolve_specs(source)
        if not specs:
            return {"ok": False, "error": f"no *.yaml or *.yml specs at {source}"}
        result = lint_mod.lint(cfg, specs)
        WorkDir().write_json("validate", "lint-report.json", result)
        return {"ok": result.status == "ok", **result.model_dump(by_alias=True)}
    except O2JError as exc:
        return _error(exc)


@mcp.tool()
def o2j_explain_finding(finding_id: str) -> dict[str, Any]:
    """Explain a validation rule: why it exists and what it breaks in the generated Java.

    Read-only. Accepts a rule name (`nullable-representation-mismatch`) or an O2J id.
    """
    doc = explain(finding_id)
    if doc is None:
        return {
            "ok": False,
            "error": f"no rulebook entry for {finding_id!r}",
            "hint": "pass the finding's `rule` value, not its message",
        }
    return {"ok": True, **doc.model_dump()}


@mcp.tool()
def o2j_normalize_spec(
    spec_path: str | None = None,
    target_version: str | None = None,
) -> dict[str, Any]:
    """Convert a spec to the canonical version the Java toolchain accepts.

    MUTATING: writes `spec.canonical.yaml` and the downgrade/sanitization reports.

    Swagger 2.0 is converted up to 3.0.4; OpenAPI 3.2 is downgraded to 3.1.2 because
    swagger-parser cannot read 3.2. A downgrade that would lose load-bearing behaviour fails
    with exit code 4 rather than producing an API that quietly lost endpoints.
    """
    try:
        result = normalize_stage.normalize(_cfg(), spec_path, target_version, WorkDir())
        return {"ok": True, **result.model_dump(by_alias=True)}
    except O2JError as exc:
        return _error(exc)


@mcp.tool()
def o2j_plan_generation(canonical_path: str | None = None) -> dict[str, Any]:
    """Build the generation plan: resources, operations, risk score and required tests.

    Read-only. Every downstream agent should read this rather than re-deriving the target.
    """
    try:
        work = WorkDir()
        result = plan_stage.plan(_cfg(), canonical_path, work)
        work.write_json("plan", "generation-plan.json", result)
        return {"ok": True, **result.model_dump(by_alias=True)}
    except O2JError as exc:
        return _error(exc)


@mcp.tool()
def o2j_generate_code(
    output_dir: str | None = None,
    run_generator: bool = True,
) -> dict[str, Any]:
    """Generate the contract layer and scaffold the hand-written layers.

    MUTATING: writes `pom.xml`, `target/generated-sources/**` and scaffold files.

    Existing hand-written files are never overwritten: a conflict produces `<name>.o2j-new`
    and is reported for human merge.
    """
    try:
        work = WorkDir()
        result = generate_stage.generate(
            _cfg(), None, output_dir, work, run_generator=run_generator
        )
        work.write_json("generate", "generation-result.json", result)
        return {"ok": True, **result.model_dump(by_alias=True)}
    except O2JError as exc:
        return _error(exc)


@mcp.tool()
def o2j_test_plan() -> dict[str, Any]:
    """The mandatory test cases per operation, derived from each operation's shape.

    Read-only. Each case is `<layer>:<case>`; a test method claims one for its operation with
    a `// o2j:<operationId>:<case>` marker comment, which `o2j_verify(mode="contract")` then
    checks against the test results.
    """
    try:
        plan = WorkDir().read_json("plan", "generation-plan.json", GenerationPlan)
    except O2JError as exc:
        return _error(exc)

    return {
        "ok": True,
        "markerFormat": "// o2j:<operationId>:<case>",
        "totalRequiredCases": sum(len(o.required_tests) for o in plan.operations),
        "operations": [
            {
                "operationId": op.operation_id,
                "method": op.method,
                "path": op.path,
                "requestType": op.request_type,
                "responseType": op.response_type,
                "requiredTests": op.required_tests,
                "markers": [f"// {op.marker(case)}" for case in op.required_tests],
                "riskFactors": op.risk_factors,
            }
            for op in plan.operations
        ],
    }


@mcp.tool()
def o2j_run_tests(test_pattern: str | None = None) -> dict[str, Any]:
    """Run the service's tests via Maven.

    Read-only with respect to source; it does write build output under `target/`.
    """
    cfg = _cfg()
    project = cfg.service_dir
    mvn = shutil.which("mvn") or shutil.which("mvn.cmd")
    if mvn is None:
        return {"ok": False, "error": "Maven not on PATH"}
    if not (project / "pom.xml").is_file():
        return {"ok": False, "error": "no pom.xml; run o2j_generate_code first"}

    command = [mvn, "-B", "-ntp", "-f", str(project / "pom.xml"), "test"]
    if test_pattern:
        command += [f"-Dtest={test_pattern}", "-DfailIfNoTests=false"]

    completed = subprocess.run(
        command, cwd=str(project), capture_output=True, text=True, timeout=1800, check=False
    )
    output = completed.stdout or completed.stderr
    failures = [
        line.strip()
        for line in output.splitlines()
        if line.strip().startswith(("[ERROR]  ", "Tests run:")) and "FAIL" in line.upper()
    ]
    return {
        "ok": completed.returncode == 0,
        "exitCode": completed.returncode,
        "failures": failures[:40],
        "tail": "\n".join(output.splitlines()[-30:]),
    }


@mcp.tool()
def o2j_coverage_report(run_build: bool = False) -> dict[str, Any]:
    """Line, branch and mutation coverage against the configured thresholds.

    Read-only unless `run_build` is set, which runs `mvn verify` and PIT first.

    A high line number with a low mutation score means the assertions are too weak, not that
    there are too few tests.
    """
    try:
        work = WorkDir()
        result = coverage_stage.coverage(_cfg(), run_build=run_build)
        work.write_json("gate", "coverage-report.json", result)
        return {"ok": result.gate.passed, **result.model_dump(by_alias=True)}
    except O2JError as exc:
        return _error(exc)


@mcp.tool()
def o2j_quality_report(tools: list[str] | None = None) -> dict[str, Any]:
    """Run the static-analysis gates and return normalized findings per tool.

    Read-only. `skipped` means disabled in config; `unavailable` means the tool could not
    run. Those are different and must not be conflated.
    """
    try:
        work = WorkDir()
        result = quality_stage.quality(_cfg(), tools)
        work.write_json("gate", "quality-report.json", result)
        return {"ok": result.gate.passed, **result.model_dump(by_alias=True)}
    except O2JError as exc:
        return _error(exc)


@mcp.tool()
def o2j_verify(mode: str = "determinism", static: bool = False) -> dict[str, Any]:
    """Check determinism, spec drift, or contract conformance.

    Read-only. `determinism` normalizes twice and diffs; `drift` compares the recorded spec
    hash with the current one; `contract` checks every required case is claimed, for its
    operation, by a test method that passed in the last `mvn verify`; `conformance` checks
    every response the tests recorded against the spec (status, content type, schema).
    `static=True` checks only that contract markers sit on test methods - useful while
    writing tests, not as a gate.
    """
    try:
        result = verify_stage.verify(_cfg(), mode, WorkDir(), require_results=not static)
        return {"ok": result.passed, **result.model_dump(by_alias=True)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except O2JError as exc:
        return _error(exc)


@mcp.tool()
def o2j_trace_operation(operation_id: str) -> dict[str, Any]:
    """Trace one operation: spec pointer, delegate, service, and its test cases.

    Read-only. Exists so an agent can ask "where is createOrder implemented?" instead of
    grepping, which keeps its context small and its answer accurate.
    """
    try:
        cfg = _cfg()
        plan = WorkDir().read_json("plan", "generation-plan.json", GenerationPlan)
    except O2JError as exc:
        return _error(exc)

    for resource in plan.resources:
        for op in resource.operations:
            if op.operation_id == operation_id:
                pkg = cfg.service.base_package.replace(".", "/")
                return {
                    "ok": True,
                    "operationId": op.operation_id,
                    "specPointer": op.pointer,
                    "generatedInterface": (
                        f"{cfg.service.base_package}.api.{resource.api_interface}"
                    ),
                    "delegate": f"src/main/java/{pkg}/api/{resource.delegate}.java",
                    "service": f"src/main/java/{pkg}/service/{resource.service}.java",
                    "requiredTests": op.required_tests,
                    "riskFactors": op.risk_factors,
                }

    known = [op.operation_id for op in plan.operations]
    return {
        "ok": False,
        "error": f"no operation {operation_id!r} in the plan",
        "knownOperationIds": known[:50],
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
