"""verify --mode determinism: two full generation runs must produce byte-identical trees."""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

import pytest

from o2j.config import O2JConfig
from o2j.stages import generate as generate_stage
from o2j.stages.ingest import ingest
from o2j.stages.report import report
from o2j.stages.verify import verify
from o2j.workdir import WorkDir
from tests.conftest import spec


@pytest.mark.parametrize("fixture", ["00-minimal", "02-petstore-3.1"])
def test_two_runs_agree_on_every_rendered_file(
    cfg: O2JConfig, work: WorkDir, fixture: str
) -> None:
    ingest(cfg, spec(fixture), work)
    result = verify(cfg, "determinism", work)
    assert result.passed, result.differences
    assert "double-generate-diff" in result.detail


def test_a_template_that_varies_between_runs_is_caught(
    cfg: O2JConfig, work: WorkDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gap the old normalize-only check had: the spec was stable, the Java was not."""
    ingest(cfg, spec("02-petstore-3.1"), work)
    counter = itertools.count()
    real_render = generate_stage.render

    def leaky(template: str, context: dict[str, Any]) -> str:
        out = real_render(template, context)
        if "public class Application" not in out:
            return out
        # Rendered once per run, so the two runs differ - as a leaked timestamp would.
        marker = f"public class Application /*{next(counter)}*/"
        return out.replace("public class Application", marker)

    monkeypatch.setattr(generate_stage, "render", leaky)
    result = verify(cfg, "determinism", work)
    assert not result.passed
    assert any(d.startswith("differs: src/main/java/") for d in result.differences)
    assert any(d.startswith("line ") for d in result.differences)


def test_manifest_records_the_determinism_result(
    cfg: O2JConfig, work: WorkDir, tmp_path: Path
) -> None:
    ingest(cfg, spec("00-minimal"), work)
    work.write_json("gate", "verify-determinism.json", verify(cfg, "determinism", work))
    manifest = report(cfg, work, root=tmp_path)
    assert manifest.determinism["verified"] is True
    assert manifest.determinism["method"] == "double-generate-diff"


def test_manifest_says_unmeasured_when_determinism_did_not_run(
    cfg: O2JConfig, work: WorkDir, tmp_path: Path
) -> None:
    manifest = report(cfg, work, root=tmp_path)
    assert manifest.determinism["verified"] is None
