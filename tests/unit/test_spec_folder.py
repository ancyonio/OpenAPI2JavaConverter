"""Input folder (openapi/) and output folder (generated_Code/) resolution."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from o2j.cli import app
from o2j.config import load_config, resolve_specs, service_slug
from o2j.errors import ConfigError

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CORPUS = REPO_ROOT / "tests" / "corpus"


def _project(tmp_path: Path, specs: dict[str, str]) -> Path:
    """A throwaway project: the reference o2j.yaml plus corpus specs under openapi/."""
    shutil.copyfile(REPO_ROOT / "o2j.yaml", tmp_path / "o2j.yaml")
    (tmp_path / "openapi").mkdir()
    for name, fixture in specs.items():
        shutil.copyfile(CORPUS / fixture / "spec.yaml", tmp_path / "openapi" / name)
    return tmp_path


def test_reference_config_reads_the_openapi_folder_and_writes_generated_code() -> None:
    cfg = load_config(REPO_ROOT / "o2j.yaml")
    assert cfg.input_path() == REPO_ROOT / "openapi"
    assert cfg.service_dir == REPO_ROOT / "generated_Code" / cfg.service.artifact_id


def test_resolve_specs_takes_yaml_and_yml_from_a_folder_in_sorted_order(tmp_path: Path) -> None:
    for name in ("b.yml", "a.yaml", "notes.md", "c.json"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "d.yaml").write_text("x", encoding="utf-8")

    assert [p.name for p in resolve_specs(tmp_path)] == ["a.yaml", "b.yml"]
    assert resolve_specs(tmp_path / "a.yaml") == [tmp_path / "a.yaml"]
    assert resolve_specs(tmp_path / "missing.yaml") == []


@pytest.mark.parametrize(
    ("name", "slug"),
    [("orders.yaml", "orders"), ("Order API.v2.yml", "order-api-v2"), ("__.yaml", "service")],
)
def test_service_slug(name: str, slug: str) -> None:
    assert service_slug(name) == slug


def test_default_spec_uses_the_only_spec_in_the_folder(tmp_path: Path) -> None:
    cfg = load_config(_project(tmp_path, {"orders.yaml": "00-minimal"}) / "o2j.yaml")
    assert cfg.default_spec() == tmp_path / "openapi" / "orders.yaml"


def test_default_spec_refuses_to_guess_between_several(tmp_path: Path) -> None:
    project = _project(tmp_path, {"a.yaml": "00-minimal", "b.yaml": "00-minimal"})
    cfg = load_config(project / "o2j.yaml")
    with pytest.raises(ConfigError, match="holds 2 specs"):
        cfg.default_spec()


def test_run_on_a_folder_plans_one_service_per_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(
        tmp_path, {"petstore.yaml": "02-petstore-3.1", "minimal.yml": "00-minimal"}
    )
    monkeypatch.chdir(project)

    result = CliRunner().invoke(app, ["run", "--config", "o2j.yaml", "--to", "plan"])

    assert result.exit_code == 0, result.output
    for slug in ("minimal", "petstore"):
        assert (project / "work" / slug / "03-plan" / "generation-plan.json").is_file()
    assert "summary" in result.output


def test_run_on_a_folder_with_only_selects_one_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(
        tmp_path, {"petstore.yaml": "02-petstore-3.1", "minimal.yml": "00-minimal"}
    )
    monkeypatch.chdir(project)

    result = CliRunner().invoke(
        app, ["run", "openapi", "--config", "o2j.yaml", "--to", "plan", "--only", "petstore"]
    )

    assert result.exit_code == 0, result.output
    assert (project / "work" / "petstore").is_dir()
    assert not (project / "work" / "minimal").exists()


def test_generate_writes_into_generated_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, {"petstore.yaml": "02-petstore-3.1"})
    monkeypatch.chdir(project)

    result = CliRunner().invoke(
        app, ["run", "--config", "o2j.yaml", "--to", "generate", "--no-run-generator"]
    )

    assert result.exit_code == 0, result.output
    service = project / "generated_Code" / "petstore"
    assert (service / "pom.xml").is_file()
    assert (service / "openapi" / "spec.canonical.yaml").is_file()
    assert not (project / "pom.xml").exists()
