"""Emit the JSON Schema for every pipeline artifact.

Generated from the Pydantic models rather than hand-written, so the schema and the code
cannot disagree. Run after changing any model:

    python -I -m o2j.schemas.generate_schemas

The schemas are what let a consumer outside Python - a Copilot agent, a CI step, another
team's tooling - validate an artifact without importing o2j.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from o2j.config import O2JConfig
from o2j.models import (
    CoverageReport,
    DowngradeReport,
    GenerationPlan,
    GenerationResult,
    Manifest,
    NormalizationResult,
    QualityReport,
    SanitizationReport,
    ValidationReport,
    VerifyReport,
)

ARTIFACTS: dict[str, type[BaseModel]] = {
    "o2j-config": O2JConfig,
    "validation-report": ValidationReport,
    "downgrade-report": DowngradeReport,
    "sanitization-report": SanitizationReport,
    "normalization-result": NormalizationResult,
    "generation-plan": GenerationPlan,
    "generation-result": GenerationResult,
    "quality-report": QualityReport,
    "coverage-report": CoverageReport,
    "verify-report": VerifyReport,
    "manifest": Manifest,
}


def main() -> None:
    out_dir = Path(__file__).resolve().parent
    for name, model in ARTIFACTS.items():
        schema = model.model_json_schema(by_alias=True)
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://o2j.dev/schemas/{name}.schema.json"
        target = out_dir / f"{name}.schema.json"
        target.write_text(
            json.dumps(schema, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(f"wrote {target.name}")


if __name__ == "__main__":
    main()
