"""`o2j lint`: run an external OpenAPI linter (Spectral or vacuum) with the house ruleset.

The blocking gate is `o2j validate`, which implements the house rules in Python and needs no
Node toolchain. This command is the second opinion: Spectral's built-in `spectral:oas` rules
(servers, contact, tag and description hygiene...) on top of the house ruleset, the same
findings developers see in their editor through `.spectral.yaml`. It is advisory unless
`--strict` is passed.

Tool resolution for `auto`: a `spectral` binary, then `vacuum` (a single Go binary that reads
Spectral rulesets), then Spectral through `npx` at the version pinned in `o2j.yaml`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Literal

from o2j.config import O2JConfig
from o2j.models import Finding, LintReport, Location, Severity
from o2j.specdoc import pointer

LintTool = Literal["auto", "spectral", "vacuum", "npx"]

# Each house rule in rules/spectral/house-api.yaml and the Python rule (Finding.rule) that
# implements it for the `o2j validate` gate. tests/unit/test_lint.py fails when either side
# gains a rule the other lacks, so the editor and the gate cannot drift apart unnoticed.
SPECTRAL_EQUIVALENTS: dict[str, str] = {
    "o2j-path-kebab-case": "path-kebab-case",
    "o2j-no-verbs-in-path": "no-verbs-in-path",
    "o2j-operation-id-required": "operation-id-required",
    "o2j-operation-id-camel-case": "operation-id-camel-case",
    "o2j-error-400-declared": "error-responses-declared",
    "o2j-error-500-declared": "error-responses-declared",
    "o2j-problem-details-media-type": "problem-details-media-type",
    "o2j-no-inline-request-schema": "inline-schema-not-allowed",
    "o2j-collection-pagination": "collection-pagination",
    "o2j-security-schemes-declared": "security-schemes-required",
    "o2j-no-nullable-in-31": "nullable-representation-mismatch",
}

PACKAGED_RULESET = Path(__file__).resolve().parent / "rules" / "spectral" / "house-api.yaml"

_SEVERITY: dict[int, Severity] = {0: "error", 1: "warn", 2: "info", 3: "info"}


def ruleset_path(cfg: O2JConfig) -> Path:
    """`validation.spectral_ruleset` relative to o2j.yaml, else the ruleset o2j ships with."""
    configured = Path(cfg.validation.spectral_ruleset)
    if not configured.is_absolute():
        configured = cfg.project_root / configured
    return configured if configured.is_file() else PACKAGED_RULESET


def resolve_tool(cfg: O2JConfig, tool: LintTool = "auto") -> tuple[str, list[str]] | None:
    """The tool name and the command prefix that runs it, or None if nothing is available."""
    spectral = shutil.which("spectral")
    vacuum = shutil.which("vacuum")
    npx = shutil.which("npx")
    npx_cmd = (
        [npx, "--yes", f"@stoplight/spectral-cli@{cfg.versions.spectral_cli}"] if npx else None
    )
    options: dict[str, tuple[str, list[str]] | None] = {
        "spectral": ("spectral", [spectral]) if spectral else None,
        "vacuum": ("vacuum", [vacuum]) if vacuum else None,
        "npx": ("spectral", npx_cmd) if npx_cmd else None,
    }
    if tool != "auto":
        return options[tool]
    return options["spectral"] or options["vacuum"] or options["npx"]


def lint(cfg: O2JConfig, specs: list[Path], tool: LintTool = "auto") -> LintReport:
    """Lint ``specs``. Never raises for findings; a missing tool is reported, not thrown."""
    ruleset = ruleset_path(cfg)
    resolved = resolve_tool(cfg, tool)
    names = [_display(cfg, s) for s in specs]
    if resolved is None:
        wanted = "Spectral, vacuum or npx" if tool == "auto" else tool
        return LintReport.build(
            tool=tool,
            ruleset=str(ruleset),
            specs=names,
            findings=[],
            status="unavailable",
            detail=f"{wanted} not found on PATH; install Node.js (for npx) or vacuum",
        )

    name, prefix = resolved
    findings: list[Finding] = []
    problems: list[str] = []
    batches = [[s] for s in specs] if name == "vacuum" else [specs]
    for batch in batches:
        if name == "vacuum":
            command = [*prefix, "spectral-report", "--ruleset", str(ruleset), "--stdout",
                       str(batch[0])]
        else:
            command = [*prefix, "lint", *map(str, batch), "--ruleset", str(ruleset),
                       "--format", "json", "--quiet"]
        try:
            completed = subprocess.run(
                command,
                cwd=str(cfg.project_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=600,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            problems.append(f"{name} could not run: {exc}")
            continue
        results = _parse(completed.stdout)
        if results is None:
            tail = (completed.stderr or completed.stdout).strip().splitlines()[-5:]
            problems.append(f"{name} exited {completed.returncode}: " + " | ".join(tail))
            continue
        findings.extend(_finding(cfg, r) for r in results)

    findings.sort(key=_order)
    return LintReport.build(
        tool=name,
        ruleset=str(ruleset),
        specs=names,
        findings=findings,
        status="unavailable" if problems and not findings else "ok",
        detail="; ".join(problems) or None,
    )


def _order(finding: Finding) -> tuple[str, int, str]:
    location = finding.location
    if location is None:
        return ("", 0, finding.rule)
    return (location.file, location.line or 0, finding.rule)


def _parse(stdout: str) -> list[dict[str, Any]] | None:
    """Spectral-format JSON results. Leading log lines (npx progress) are skipped."""
    start = stdout.find("[")
    if start < 0:
        return [] if not stdout.strip() else None
    try:
        data = json.loads(stdout[start:])
    except json.JSONDecodeError:
        return None
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else None


def _finding(cfg: O2JConfig, result: dict[str, Any]) -> Finding:
    code = str(result.get("code", "unknown"))
    start = (result.get("range") or {}).get("start") or {}
    line = start.get("line")
    column = start.get("character")
    path_tokens = [str(t) for t in (result.get("path") or [])]
    equivalent = SPECTRAL_EQUIVALENTS.get(code)
    return Finding(
        id="O2J-L-001",
        rule=code,
        severity=_SEVERITY.get(int(result.get("severity", 1)), "warn"),
        message=str(result.get("message", "")).strip(),
        pointer=pointer(*path_tokens),
        location=Location(
            file=_display(cfg, Path(str(result.get("source", "")))),
            line=int(line) + 1 if isinstance(line, int) else None,
            column=int(column) + 1 if isinstance(column, int) else None,
        ),
        remediation=(
            f"House rule; `o2j validate` enforces it as `{equivalent}`."
            if equivalent
            else result.get("documentationUrl")
        ),
        auto_fixable=False,
    )


def _display(cfg: O2JConfig, path: Path) -> str:
    """A path relative to the project root when possible, with forward slashes."""
    try:
        return path.resolve().relative_to(cfg.project_root.resolve()).as_posix()
    except (ValueError, OSError):
        return path.as_posix()
