"""S8 Report - the manifest, the traceability matrix and the PR body.

The manifest is the audit record: given it, anyone can say which spec produced which code,
with which tool versions, at what quality. The traceability matrix answers the question that
comes up in every review of generated code - "where is this operation actually implemented,
and what tests cover it?"
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from o2j.config import O2JConfig
from o2j.models import (
    CoverageReport,
    GenerationPlan,
    Manifest,
    QualityReport,
)
from o2j.workdir import WorkDir

T = TypeVar("T", bound=BaseModel)


def report(
    cfg: O2JConfig,
    work: WorkDir | None = None,
    root: str | Path | None = None,
) -> Manifest:
    """Assemble the manifest and the human-readable reports."""
    work = work or WorkDir()
    project = Path(root) if root else cfg.service_dir

    plan = _try_read(work, "plan", "generation-plan.json", GenerationPlan)
    quality = _try_read(work, "gate", "quality-report.json", QualityReport)
    cov = _try_read(work, "gate", "coverage-report.json", CoverageReport)

    generated_count = 0
    generated_root = project / cfg.generation.output_dir
    if generated_root.is_dir():
        generated_count = sum(1 for _ in generated_root.rglob("*.java"))

    hand_written = 0
    for subdir in ("src/main/java", "src/test/java"):
        tree = project / subdir
        if tree.is_dir():
            hand_written += sum(1 for _ in tree.rglob("*.java"))

    manifest = Manifest(
        spec={
            "canonicalHash": plan.spec_hash if plan else None,
            "declaredVersion": plan.original_version if plan else None,
            "canonicalVersion": cfg.openapi.canonical_version,
        },
        target={
            "javaVersion": cfg.target.java_version,
            "javaTier": cfg.target.tier,
            "springBoot": cfg.spring_boot_version,
            "springProfile": cfg.target.spring_profile,
            "buildTool": cfg.target.build_tool,
            "webStack": cfg.target.web_stack,
        },
        tools={
            "o2j": "1.0.0",
            "openapiGenerator": cfg.versions.openapi_generator,
        },
        operations=len(plan.operations) if plan else 0,
        generated_files=generated_count,
        hand_written_files=hand_written,
        quality={
            "lineCoverage": cov.line_coverage if cov else None,
            "branchCoverage": cov.branch_coverage if cov else None,
            "serviceLineCoverage": cov.service_line_coverage if cov else None,
            "mutationScore": cov.mutation_score if cov else None,
            "gates": {t.tool: t.status for t in quality.tools} if quality else {},
        },
        determinism={"verified": None, "method": "double-normalize-diff"},
    )

    work.write_json("report", "manifest.json", manifest)
    (project / "o2j.lock.json").write_text(
        json.dumps(manifest.model_dump(by_alias=True), indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    if plan:
        work.write_text("report", "traceability.md", _traceability(cfg, plan))
        work.write_text("report", "pr-body.md", _pr_body(cfg, plan, cov, quality))

    return manifest


def _try_read(work: WorkDir, stage: str, name: str, model: type[T]) -> T | None:
    """Read an upstream artifact, or None if that stage has not run.

    `o2j report` is deliberately runnable after a partial pipeline, so a missing coverage or
    quality artifact is an expected state, not an error. The manifest then records those
    fields as unmeasured rather than claiming a result it does not have.
    """
    try:
        return work.read_json(stage, name, model)
    except Exception:
        return None


def _traceability(cfg: O2JConfig, plan: GenerationPlan) -> str:
    """Operation -> delegate -> service -> tests, for every operation (FR-G-10)."""
    lines = [
        "# Traceability matrix",
        "",
        f"Spec: `{plan.spec_hash}`  ",
        f"Target: Java {plan.target.java_version} (tier {plan.target.java_tier}), "
        f"Spring Boot {plan.target.spring_boot_version}  ",
        f"Operations: {len(plan.operations)}",
        "",
    ]

    for resource in plan.resources:
        lines += [
            f"## `{resource.tag}`",
            "",
            f"Generated interface `{resource.api_interface}` -> delegate "
            f"`{resource.delegate}` -> service `{resource.service}`",
            "",
            "| operationId | method | path | request | response | required tests |",
            "|---|---|---|---|---|---|",
        ]
        for op in resource.operations:
            lines.append(
                f"| `{op.operation_id}` | {op.method} | `{op.path}` | "
                f"{op.request_type or '-'} | {op.response_type or '-'} | "
                f"{len(op.required_tests)} |"
            )
        lines.append("")

    lines += ["## Required test cases", ""]
    for resource in plan.resources:
        for op in resource.operations:
            lines.append(f"### `{op.operation_id}`")
            lines.append("")
            for case in op.required_tests:
                lines.append(f"- `{op.marker(case)}`")
            lines.append("")

    return "\n".join(lines)


def _pr_body(
    cfg: O2JConfig,
    plan: GenerationPlan,
    cov: CoverageReport | None,
    quality: QualityReport | None,
) -> str:
    coverage_line = "not measured"
    if cov is not None:
        parts = []
        if cov.line_coverage is not None:
            parts.append(f"line {cov.line_coverage:.1f}%")
        if cov.branch_coverage is not None:
            parts.append(f"branch {cov.branch_coverage:.1f}%")
        if cov.mutation_score is not None:
            parts.append(f"mutation {cov.mutation_score:.1f}%")
        coverage_line = ", ".join(parts) or "not measured"

    gate_lines: list[str] = []
    if quality is not None:
        for tool in quality.tools:
            icon = {"pass": "ok", "fail": "FAIL", "skipped": "skipped", "unavailable": "n/a"}[
                tool.status
            ]
            gate_lines.append(f"- `{tool.tool}`: {icon}")

    risk_note = ""
    if plan.risk_score.band == "high":
        risk_note = (
            "\n> **Risk band: high.** "
            + "; ".join(plan.risk_score.drivers)
            + ". Review the generated model before trusting the business logic built on it.\n"
        )

    return "\n".join(
        [
            f"# Generate {cfg.service.name} from OpenAPI",
            "",
            f"Generated from spec `{plan.spec_hash}` "
            f"(OpenAPI {plan.original_version} -> {plan.spec_version}).",
            "",
            f"- Operations: **{len(plan.operations)}** across {len(plan.resources)} resources",
            f"- Target: Java {plan.target.java_version} (tier {plan.target.java_tier}), "
            f"Spring Boot {plan.target.spring_boot_version}, {plan.target.build_tool}, "
            f"{plan.target.web_stack}",
            f"- Coverage: {coverage_line}",
            f"- Risk: {plan.risk_score.band} ({plan.risk_score.value}/100)",
            risk_note,
            "## Gates",
            "",
            *(gate_lines or ["- not run"]),
            "",
            "## Review notes",
            "",
            "- Contract code under `target/generated-sources/` is build output. Do not review "
            "it as source and do not edit it; change the spec or the template instead.",
            "- Hand-written layers are in `src/main/java`. Generated DTOs must not appear "
            "outside the `api` and `mapper` packages.",
            "- The traceability matrix maps every operation to its delegate, service and "
            "required test cases.",
        ]
    )
