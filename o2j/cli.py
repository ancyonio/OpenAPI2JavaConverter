"""The CLI adapter.

Thin by design: every command resolves configuration, calls one stage function, writes the
artifact and maps the outcome to an exit code. No logic lives here, so the CLI and the MCP
server cannot drift apart.

Convention: structured JSON on stdout, human-readable text on stderr. Agents parse stdout,
people read stderr, and piping to `jq` never has to strip prose.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import BaseModel

from o2j import __version__
from o2j.config import O2JConfig, load_config, resolve_specs, service_slug
from o2j.errors import ExitCode, O2JError
from o2j.models import GenerationPlan
from o2j.stages import coverage as coverage_stage
from o2j.stages import doctor as doctor_stage
from o2j.stages import generate as generate_stage
from o2j.stages import ingest as ingest_stage
from o2j.stages import normalize as normalize_stage
from o2j.stages import plan as plan_stage
from o2j.stages import quality as quality_stage
from o2j.stages import report as report_stage
from o2j.stages import skills as skills_stage
from o2j.stages import validate as validate_stage
from o2j.stages import verify as verify_stage
from o2j.workdir import WorkDir

app = typer.Typer(
    name="o2j",
    help="Convert an OpenAPI description into a Java Spring Boot service.",
    add_completion=False,
    no_args_is_help=True,
)

ConfigOpt = Annotated[
    Path | None, typer.Option("--config", "-c", help="Path to o2j.yaml.")
]
WorkOpt = Annotated[
    Path | None, typer.Option("--work-dir", "-w", help="Stage artifact directory.")
]
JsonOpt = Annotated[bool, typer.Option("--json", help="Emit JSON on stdout.")]


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def _emit(model: BaseModel, as_json: bool) -> None:
    if as_json:
        print(json.dumps(model.model_dump(by_alias=True), indent=2, ensure_ascii=False))


def _fail(exc: O2JError) -> None:
    _err(f"error: {exc.message}")
    if exc.hint:
        _err(f"hint:  {exc.hint}")
    raise typer.Exit(int(exc.exit_code))


def _load(config: Path | None) -> Any:
    try:
        return load_config(config)
    except O2JError as exc:
        _fail(exc)


@app.command()
def version() -> None:
    """Print the o2j version."""
    print(__version__)


@app.command()
def init(
    service: Annotated[str, typer.Option(help="Service name.")] = "my-service",
    base_package: Annotated[str, typer.Option(help="Java base package.")] = "com.example.app",
    java: Annotated[int, typer.Option(help="Target Java version.")] = 21,
) -> None:
    """Write a starter o2j.yaml into the current directory."""
    target = Path("o2j.yaml")
    if target.exists():
        _err("o2j.yaml already exists; refusing to overwrite it")
        raise typer.Exit(int(ExitCode.USAGE))

    template = Path(__file__).resolve().parent.parent / "o2j.yaml"
    if not template.is_file():
        _err("packaged o2j.yaml template not found")
        raise typer.Exit(int(ExitCode.INTERNAL_ERROR))

    text = template.read_text(encoding="utf-8")
    text = text.replace("name: order-service", f"name: {service}")
    text = text.replace("artifact_id: order-service", f"artifact_id: {service}")
    text = text.replace(
        "base_package: com.acme.commerce.order", f"base_package: {base_package}"
    )
    text = text.replace("java_version: 21", f"java_version: {java}")
    target.write_text(text, encoding="utf-8", newline="\n")
    _err(f"wrote {target}")


@app.command()
def doctor(
    config: ConfigOpt = None,
    check_versions: Annotated[
        bool, typer.Option("--check-versions", help="Query Maven Central for newer pins.")
    ] = False,
    as_json: JsonOpt = False,
) -> None:
    """Verify prerequisites, and optionally report pinned-version drift."""
    cfg = _load(config)
    result = doctor_stage.doctor(cfg, check_versions=check_versions)

    for check in result.checks:
        label = {"ok": "ok  ", "warn": "warn", "missing": "MISS", "unknown": "?   "}[check.status]
        _err(f"[{label}] {check.name}: {check.detail}")
        if check.remedy and check.status != "ok":
            _err(f"         -> {check.remedy}")

    for drift in result.drift:
        if drift.drifted:
            _err(f"[warn] {drift.key}: pinned {drift.pinned}, latest {drift.latest}")
        elif drift.note:
            _err(f"[?   ] {drift.key}: {drift.note}")
        else:
            _err(f"[ok  ] {drift.key}: {drift.pinned} is current")

    _emit(result, as_json)
    raise typer.Exit(0 if result.ok else int(ExitCode.USAGE))


@app.command()
def ingest(
    source: Annotated[Path | None, typer.Argument(help="Spec path.")] = None,
    config: ConfigOpt = None,
    work_dir: WorkOpt = None,
    as_json: JsonOpt = False,
) -> None:
    """S0 - copy the spec into quarantine and record its hash."""
    cfg = _load(config)
    try:
        result = ingest_stage.ingest(cfg, source, WorkDir(work_dir))
    except O2JError as exc:
        _fail(exc)
    _err(
        f"ingested {result.source} ({result.size_bytes} bytes, OpenAPI "
        f"{result.detected_version})"
    )
    _emit(result, as_json)


@app.command()
def validate(
    spec: Annotated[Path | None, typer.Argument(help="Spec path.")] = None,
    config: ConfigOpt = None,
    work_dir: WorkOpt = None,
    as_json: JsonOpt = False,
    sarif: Annotated[bool, typer.Option("--sarif", help="Also write a SARIF report.")] = True,
) -> None:
    """S1 - validate syntax, semantics and house API standards."""
    cfg = _load(config)
    work = WorkDir(work_dir)
    try:
        result = validate_stage.validate(cfg, spec, work)
    except O2JError as exc:
        _fail(exc)

    work.write_json("validate", "validation-report.json", result)
    if sarif:
        payload = validate_stage.to_sarif(result, str(spec or cfg.openapi.input))
        work.write_text("validate", "validation-report.sarif", json.dumps(payload, indent=2))

    for finding in result.findings:
        where = ""
        if finding.location and finding.location.line:
            where = f"{finding.location.file}:{finding.location.line}"
        else:
            where = finding.pointer
        _err(f"{finding.severity:5} {finding.id} {where}  {finding.message}")
        if finding.remediation:
            _err(f"      fix: {finding.remediation}")

    s = result.summary
    _err(
        f"\n{s.errors} error(s), {s.warnings} warning(s), {s.infos} info "
        f"- gate {'passed' if result.gate.passed else 'FAILED'}"
    )
    _emit(result, as_json)

    if not result.gate.passed:
        raise typer.Exit(int(ExitCode.VALIDATION_FAILED))


@app.command()
def normalize(
    spec: Annotated[Path | None, typer.Argument(help="Spec path.")] = None,
    config: ConfigOpt = None,
    work_dir: WorkOpt = None,
    target_version: Annotated[str | None, typer.Option(help="Canonical version.")] = None,
    as_json: JsonOpt = False,
) -> None:
    """S2 - convert the spec to the canonical version the Java toolchain accepts."""
    cfg = _load(config)
    try:
        result = normalize_stage.normalize(cfg, spec, target_version, WorkDir(work_dir))
    except O2JError as exc:
        _fail(exc)

    _err(f"{result.source_version} -> {result.target_version}")
    for step in result.normalizations:
        _err(f"  applied: {step}")
    if result.downgrade_report:
        for entry in result.downgrade_report.entries:
            _err(
                f"  {entry.classification}: {entry.construct_name} at "
                f"{entry.pointer} ({entry.action})"
            )
    if result.sanitization_report:
        for redaction in result.sanitization_report.redactions:
            _err(f"  redacted {redaction.pattern} at {redaction.pointer}")
    _err(f"canonical: {result.canonical_path}\nhash: {result.canonical_hash}")
    _emit(result, as_json)


@app.command()
def plan(
    canonical: Annotated[Path | None, typer.Argument(help="Canonical spec path.")] = None,
    config: ConfigOpt = None,
    work_dir: WorkOpt = None,
    as_json: JsonOpt = False,
) -> None:
    """S3 - build the operation inventory and the required-test matrix."""
    cfg = _load(config)
    work = WorkDir(work_dir)
    try:
        result = plan_stage.plan(cfg, canonical, work)
    except O2JError as exc:
        _fail(exc)

    work.write_json("plan", "generation-plan.json", result)
    _err(
        f"{len(result.operations)} operation(s) across {len(result.resources)} resource(s); "
        f"risk {result.risk_score.band} ({result.risk_score.value}/100)"
    )
    for driver in result.risk_score.drivers:
        _err(f"  risk driver: {driver}")
    _err(f"required test cases: {sum(len(o.required_tests) for o in result.operations)}")
    _emit(result, as_json)


@app.command()
def testplan(
    config: ConfigOpt = None,
    work_dir: WorkOpt = None,
    as_json: JsonOpt = False,
) -> None:
    """S6 - print the required test cases per operation."""
    _load(config)  # validate configuration even though this stage only reads artifacts
    work = WorkDir(work_dir)
    try:
        generation_plan = work.read_json("plan", "generation-plan.json", GenerationPlan)
    except O2JError as exc:
        _fail(exc)

    for operation in generation_plan.operations:
        _err(f"{operation.operation_id} ({operation.method} {operation.path})")
        for case in operation.required_tests:
            _err(f"  // {operation.marker(case)}")
    _emit(generation_plan, as_json)


@app.command()
def generate(
    config: ConfigOpt = None,
    work_dir: WorkOpt = None,
    output: Annotated[Path | None, typer.Option(help="Service root.")] = None,
    run_generator: Annotated[
        bool,
        typer.Option(
            "--run-generator/--no-run-generator",
            help="Invoke openapi-generator via Maven, or scaffold only.",
        ),
    ] = True,
    as_json: JsonOpt = False,
) -> None:
    """S4 - generate the contract layer and scaffold the hand-written layers."""
    cfg = _load(config)
    work = WorkDir(work_dir)
    try:
        result = generate_stage.generate(
            cfg, None, output, work, run_generator=run_generator
        )
    except O2JError as exc:
        _fail(exc)

    work.write_json("generate", "generation-result.json", result)
    _err(f"generated {len(result.files)} contract file(s)")
    _err(
        f"required test cases: {result.generated_cases} generated by o2j, "
        f"{result.pending_cases} left as disabled skeletons"
    )
    for path in result.managed:
        _err(f"  managed    {path}")
    for path in result.scaffolded:
        _err(f"  scaffolded {path}")
    for path in result.conflicts:
        _err(f"  CONFLICT   {path} (wrote {path}.o2j-new; merge by hand)")
    _emit(result, as_json)


@app.command()
def quality(
    config: ConfigOpt = None,
    work_dir: WorkOpt = None,
    tools: Annotated[list[str] | None, typer.Option("--tool", help="Limit to these tools.")] = None,
    as_json: JsonOpt = False,
) -> None:
    """S7 - run the static-analysis gates."""
    cfg = _load(config)
    work = WorkDir(work_dir)
    result = quality_stage.quality(cfg, tools)
    work.write_json("gate", "quality-report.json", result)

    for tool in result.tools:
        counted = f" ({tool.findings} finding(s))" if tool.findings else ""
        _err(f"[{tool.status:11}] {tool.tool}{counted}")
        if tool.detail and tool.status in ("fail", "unavailable"):
            for line in tool.detail.splitlines()[:5]:
                _err(f"              {line}")

    _emit(result, as_json)
    if not result.gate.passed:
        _err(f"\nquality gate FAILED: {result.gate.failed_on}")
        raise typer.Exit(int(ExitCode.GATE_FAILED))


@app.command()
def coverage(
    config: ConfigOpt = None,
    work_dir: WorkOpt = None,
    run_build: Annotated[
        bool, typer.Option("--run-build/--no-run-build", help="Run `mvn verify` first.")
    ] = True,
    as_json: JsonOpt = False,
) -> None:
    """S7 - evaluate the coverage and mutation thresholds."""
    cfg = _load(config)
    work = WorkDir(work_dir)
    result = coverage_stage.coverage(cfg, run_build=run_build)
    work.write_json("gate", "coverage-report.json", result)

    def fmt(value: float | None) -> str:
        return f"{value:.1f}%" if value is not None else "n/a"

    _err(f"line      {fmt(result.line_coverage)} (min {result.thresholds['line_min']}%)")
    _err(f"branch    {fmt(result.branch_coverage)} (min {result.thresholds['branch_min']}%)")
    _err(
        f"service   {fmt(result.service_line_coverage)} "
        f"(min {result.thresholds['service_layer_line_min']}%)"
    )
    _err(
        f"mutation  {fmt(result.mutation_score)} "
        f"(min {result.thresholds['mutation_threshold']}%)"
    )

    _emit(result, as_json)
    if not result.gate.passed:
        _err(f"\ncoverage gate FAILED: {result.gate.failed_on}")
        raise typer.Exit(int(ExitCode.GATE_FAILED))


@app.command()
def verify(
    mode: Annotated[
        str, typer.Option(help="determinism | drift | contract | conformance")
    ] = "determinism",
    config: ConfigOpt = None,
    work_dir: WorkOpt = None,
    static: Annotated[
        bool,
        typer.Option(
            "--static",
            help="contract: check markers sit on test methods, without requiring test results.",
        ),
    ] = False,
    as_json: JsonOpt = False,
) -> None:
    """S7 - determinism, spec drift, or contract conformance."""
    cfg = _load(config)
    try:
        result = verify_stage.verify(
            cfg, mode, WorkDir(work_dir), require_results=not static
        )
    except ValueError as exc:
        _err(f"error: {exc}")
        raise typer.Exit(int(ExitCode.USAGE)) from exc
    except O2JError as exc:
        _fail(exc)

    _err(f"{mode}: {'passed' if result.passed else 'FAILED'} - {result.detail}")
    for difference in result.differences:
        _err(f"  {difference}")

    _emit(result, as_json)
    if not result.passed:
        raise typer.Exit(int(ExitCode.DRIFT_FAILED))


@app.command()
def report(
    config: ConfigOpt = None,
    work_dir: WorkOpt = None,
    as_json: JsonOpt = False,
) -> None:
    """S8 - write the manifest, traceability matrix and PR body."""
    cfg = _load(config)
    work = WorkDir(work_dir)
    result = report_stage.report(cfg, work)
    _err(
        f"manifest written: {result.operations} operation(s), "
        f"{result.generated_files} generated file(s), "
        f"{result.hand_written_files} hand-written file(s)"
    )
    _emit(result, as_json)


STAGE_ORDER = ["ingest", "validate", "normalize", "plan", "generate", "gate", "report"]


@app.command()
def run(
    spec: Annotated[
        Path | None,
        typer.Argument(
            help="A spec file, or a folder whose *.yaml/*.yml files each become a service. "
            "Defaults to `openapi.input` (the openapi/ folder)."
        ),
    ] = None,
    config: ConfigOpt = None,
    work_dir: WorkOpt = None,
    java: Annotated[int | None, typer.Option(help="Override the target Java version.")] = None,
    from_stage: Annotated[str, typer.Option("--from", help="First stage to run.")] = "ingest",
    to_stage: Annotated[str, typer.Option("--to", help="Last stage to run.")] = "report",
    only: Annotated[
        list[str] | None,
        typer.Option(
            "--only", help="Folder mode: convert only these specs (file name or service name)."
        ),
    ] = None,
    run_generator: Annotated[
        bool, typer.Option("--run-generator/--no-run-generator")
    ] = True,
) -> None:
    """Run the pipeline from one stage to another, stopping at the first failed gate.

    Given a folder, every *.yaml / *.yml in it is converted into its own service under
    `generation.service_root` (generated_Code/), named after the spec file, with its stage
    artifacts in `<work-dir>/<service>/`. A failed spec does not stop the others.
    """
    cfg = _load(config)
    if java is not None:
        cfg.target.java_version = java

    if from_stage not in STAGE_ORDER or to_stage not in STAGE_ORDER:
        _err(f"error: stages must be one of {', '.join(STAGE_ORDER)}")
        raise typer.Exit(int(ExitCode.USAGE))
    selected = STAGE_ORDER[STAGE_ORDER.index(from_stage) : STAGE_ORDER.index(to_stage) + 1]

    source = spec if spec is not None else cfg.input_path()
    if not source.is_dir():
        # One spec: the service identity comes from o2j.yaml, as for the stage commands.
        code = _run_one(cfg, spec, WorkDir(work_dir), selected, run_generator)
        if code:
            raise typer.Exit(code)
        _err(f"\npipeline complete -> {cfg.service_dir}")
        return

    specs = resolve_specs(source)
    if only:
        wanted = set(only)
        specs = [s for s in specs if {s.name, s.stem, service_slug(s)} & wanted]
    if not specs:
        _err(f"error: no *.yaml or *.yml specs in {source}")
        raise typer.Exit(int(ExitCode.USAGE))

    slugs = [service_slug(s) for s in specs]
    clashes = sorted({slug for slug in slugs if slugs.count(slug) > 1})
    if clashes:
        _err(f"error: several specs map to the same service name: {', '.join(clashes)}")
        raise typer.Exit(int(ExitCode.USAGE))

    work_root = WorkDir(work_dir).root
    outcomes: list[tuple[str, int]] = []
    for path in specs:
        slug = service_slug(path)
        spec_cfg = cfg.model_copy(deep=True)
        spec_cfg.service.name = slug
        spec_cfg.service.artifact_id = slug
        if len(specs) > 1 and spec_cfg.ems.enabled:
            # EMS routes name the operations of one spec; applied to every spec they would fail.
            _err(f"[{slug}] ems mapping skipped: it applies to single-spec runs only")
            spec_cfg.ems.enabled = False
        _err(f"\n=== {path.name} -> {spec_cfg.service_dir}")
        code = _run_one(spec_cfg, path, WorkDir(work_root / slug), selected, run_generator)
        outcomes.append((slug, code))

    _err("\nsummary")
    for slug, code in outcomes:
        _err(f"  {slug:30} {'ok' if code == 0 else f'FAILED (exit {code})'}")
    failed = [code for _, code in outcomes if code]
    if failed:
        raise typer.Exit(max(failed))
    root = cfg.project_root / cfg.generation.service_root
    _err(f"\npipeline complete: {len(outcomes)} service(s) in {root}")


def _run_one(
    cfg: O2JConfig, spec: Path | None, work: WorkDir, selected: list[str], run_generator: bool
) -> int:
    """Run the selected stages for one spec; return its exit code (0 when every gate passes)."""
    try:
        if "ingest" in selected:
            ingested = ingest_stage.ingest(cfg, spec, work)
            _err(f"[S0] ingest     {ingested.detected_version} ({ingested.size_bytes} bytes)")

        if "validate" in selected:
            validation = validate_stage.validate(cfg, spec, work)
            work.write_json("validate", "validation-report.json", validation)
            _err(
                f"[S1] validate   {validation.summary.errors} error(s), "
                f"{validation.summary.warnings} warning(s)"
            )
            if not validation.gate.passed:
                _err("     gate FAILED - fix the spec before generating")
                return int(ExitCode.VALIDATION_FAILED)

        if "normalize" in selected:
            normalized = normalize_stage.normalize(cfg, spec, None, work)
            _err(
                f"[S2] normalize  {normalized.source_version} -> "
                f"{normalized.target_version} ({len(normalized.normalizations)} step(s))"
            )

            # The canonical document is what code is generated from, so it is what has to meet
            # the standard. This re-validation is also where a converted Swagger 2.0 document
            # gets its first full semantic and house-standard check (see stages/validate.py).
            if "validate" in selected:
                canonical_check = validate_stage.validate(
                    cfg, normalized.canonical_path, work
                )
                work.write_json(
                    "validate", "validation-report.canonical.json", canonical_check
                )
                _err(
                    f"[S2] re-check   {canonical_check.summary.errors} error(s), "
                    f"{canonical_check.summary.warnings} warning(s) on the canonical spec"
                )
                if not canonical_check.gate.passed:
                    for finding in canonical_check.findings:
                        if finding.severity == "error":
                            _err(f"     {finding.rule} {finding.pointer}: {finding.message}")
                    _err("     gate FAILED on the canonical spec")
                    return int(ExitCode.VALIDATION_FAILED)

        if "plan" in selected:
            planned = plan_stage.plan(cfg, None, work)
            work.write_json("plan", "generation-plan.json", planned)
            _err(
                f"[S3] plan       {len(planned.operations)} operation(s), risk "
                f"{planned.risk_score.band}"
            )
            if planned.risk_score.band == "high":
                _err(
                    "     risk band is high; review the generated model before building "
                    "logic on it"
                )

        if "generate" in selected:
            generated = generate_stage.generate(
                cfg, None, None, work, run_generator=run_generator
            )
            work.write_json("generate", "generation-result.json", generated)
            _err(
                f"[S4] generate   {len(generated.files)} contract file(s), "
                f"{len(generated.scaffolded)} scaffolded, "
                f"{len(generated.conflicts)} conflict(s) -> {cfg.service_dir}"
            )
            _err(
                f"[S4] tests      {generated.generated_cases} required case(s) generated, "
                f"{generated.pending_cases} left as disabled skeletons"
            )

            determinism = verify_stage.verify(cfg, "determinism", work)
            _err(f"[S4] determinism {'passed' if determinism.passed else 'FAILED'}")
            if not determinism.passed:
                return int(ExitCode.DRIFT_FAILED)

        if "gate" in selected:
            quality_report = quality_stage.quality(cfg)
            work.write_json("gate", "quality-report.json", quality_report)
            coverage_report = coverage_stage.coverage(cfg)
            work.write_json("gate", "coverage-report.json", coverage_report)
            contract = verify_stage.verify(cfg, "contract", work)
            conforms = verify_stage.verify(cfg, "conformance", work)

            def verdict(ok: bool) -> str:
                return "passed" if ok else "FAILED"

            _err(
                f"[S7] gates      quality {verdict(quality_report.gate.passed)}, coverage "
                f"{verdict(coverage_report.gate.passed)}, required tests "
                f"{verdict(contract.passed)}, conformance {verdict(conforms.passed)}"
            )
            for detail in (contract, conforms):
                if not detail.passed:
                    _err(f"     {detail.mode}: {detail.detail}")
            if not all(
                (quality_report.gate.passed, coverage_report.gate.passed,
                 contract.passed, conforms.passed)
            ):
                return int(ExitCode.GATE_FAILED)

        if "report" in selected:
            manifest = report_stage.report(cfg, work)
            _err(f"[S8] report     {manifest.operations} operation(s) recorded")

    except O2JError as exc:
        _err(f"error: {exc.message}")
        if exc.hint:
            _err(f"hint:  {exc.hint}")
        return int(exc.exit_code)

    return 0


skills_app = typer.Typer(
    help="Publish the root `skills/` directory to the paths each agent runtime scans.",
    no_args_is_help=True,
)
app.add_typer(skills_app, name="skills")


@skills_app.command("sync")
def skills_sync(
    root: Annotated[Path, typer.Option(help="Project root.")] = Path("."),
    as_json: JsonOpt = False,
) -> None:
    """Copy `skills/` into the runtime location(s) Copilot reads."""
    try:
        result = skills_stage.sync(root)
    except O2JError as exc:
        _fail(exc)

    _err(f"{len(result.skills)} skill(s) from {result.canonical}/")
    for mirror in result.mirrors:
        _err(f"  -> {mirror}")
    _err(f"{result.files_written} file(s) written, {len(result.removed)} stale file(s) removed")
    for path in result.removed:
        _err(f"  removed {path}")
    _emit(result, as_json)


@skills_app.command("check")
def skills_check(
    root: Annotated[Path, typer.Option(help="Project root.")] = Path("."),
    as_json: JsonOpt = False,
) -> None:
    """Verify every runtime mirror is byte-identical to `skills/`."""
    try:
        result = skills_stage.check(root)
    except O2JError as exc:
        _fail(exc)

    if result.in_sync:
        _err("skills: all runtime mirrors are in sync")
    else:
        _err("skills: mirrors are OUT OF SYNC - run `o2j skills sync`")
        for difference in result.differences:
            _err(f"  {difference}")

    _emit(result, as_json)
    if not result.in_sync:
        raise typer.Exit(int(ExitCode.DRIFT_FAILED))


if __name__ == "__main__":
    app()
