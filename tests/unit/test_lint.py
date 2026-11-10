"""OpenAPI schema check (FR-V-03), breaking changes (FR-V-11), `o2j lint`, Sonar wiring."""

from __future__ import annotations

import copy
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML
from typer.testing import CliRunner

from o2j import lint as lint_mod
from o2j.cli import app
from o2j.config import O2JConfig, load_config
from o2j.rulebook import explain
from o2j.rules.breaking import compare
from o2j.specdoc import load_spec
from o2j.stages import quality as quality_stage
from o2j.stages.validate import validate
from o2j.workdir import WorkDir

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CORPUS = REPO_ROOT / "tests" / "corpus"
PETSTORE = CORPUS / "02-petstore-3.1" / "spec.yaml"


def _plain(path: Path) -> dict[str, Any]:
    return json.loads(json.dumps(load_spec(path), default=str))


def _write(path: Path, doc: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml = YAML()
    yaml.default_flow_style = False
    with path.open("w", encoding="utf-8") as fh:
        yaml.dump(doc, fh)
    return path


def _rules(changes: list[Any]) -> set[str]:
    return {c.rule for c in changes}


# ------------------------------------------------------------------ FR-V-03 schema


class TestOpenApiSchema:
    @pytest.mark.parametrize(
        "fixture", ["00-minimal", "01-petstore-3.0", "02-petstore-3.1", "03-swagger-2.0"]
    )
    def test_valid_corpus_specs_have_no_schema_errors(
        self, cfg: O2JConfig, work: WorkDir, fixture: str
    ) -> None:
        report = validate(cfg, CORPUS / fixture / "spec.yaml", work, check_breaking=False)
        assert not [f for f in report.findings if f.rule == "openapi-schema-invalid"]

    def test_structural_errors_are_reported_with_location(
        self, cfg: O2JConfig, work: WorkDir, tmp_path: Path
    ) -> None:
        doc = _plain(PETSTORE)
        get = doc["paths"]["/orders"]["get"]
        get["paramters"] = get.pop("parameters")  # misspelt keyword
        spec = _write(tmp_path / "broken.yaml", doc)

        report = validate(cfg, spec, work, check_breaking=False)

        schema = [f for f in report.findings if f.rule == "openapi-schema-invalid"]
        assert schema, "a misspelt keyword must fail the OpenAPI schema check"
        assert all(f.id == "O2J-V-003" and f.severity == "error" for f in schema)
        assert any("/paths/~1orders/get" in f.pointer for f in schema)
        assert all(f.location and f.location.line for f in schema)
        assert not report.gate.passed

    def test_32_is_deferred_not_failed(self, cfg: O2JConfig, work: WorkDir) -> None:
        report = validate(cfg, CORPUS / "04-oas-3.2" / "spec.yaml", work, check_breaking=False)
        deferred = [f for f in report.findings if f.rule == "openapi-schema-deferred"]
        assert len(deferred) == 1 and deferred[0].severity == "info"


# ------------------------------------------------------------------ FR-V-11 breaking


class TestBreakingChanges:
    def test_identical_specs_have_no_changes(self) -> None:
        doc = _plain(PETSTORE)
        assert compare(doc, copy.deepcopy(doc)) == []

    def test_removed_operation(self) -> None:
        base = _plain(PETSTORE)
        new = copy.deepcopy(base)
        del new["paths"]["/orders/{orderId}"]["delete"]
        changes = compare(base, new)
        assert _rules(changes) == {"operation-removed"}
        assert changes[0].breaking

    def test_renamed_path_parameter_is_not_a_change(self) -> None:
        base = _plain(PETSTORE)
        new = copy.deepcopy(base)
        item = new["paths"].pop("/orders/{orderId}")
        for op in item.values():
            for param in op.get("parameters", []) if isinstance(op, dict) else []:
                if param.get("name") == "orderId":
                    param["name"] = "id"
        new["paths"]["/orders/{id}"] = item
        assert compare(base, new) == []

    def test_response_and_request_directions(self) -> None:
        base = _plain(PETSTORE)
        new = copy.deepcopy(base)
        schemas = new["components"]["schemas"]
        schemas["Order"]["required"].remove("total")             # response: no longer guaranteed
        schemas["Order"]["properties"]["id"]["type"] = ["string", "null"]  # response widened
        schemas["CreateOrderRequest"]["required"].append("status")  # request: newly required
        schemas["OrderStatus"]["enum"].remove("CANCELLED")       # request: value rejected
        schemas["OrderItem"]["properties"]["quantity"]["maximum"] = 99  # request: tightened

        rules = _rules(compare(base, new))

        assert {
            "response-property-became-optional",
            "response-type-widened",
            "request-property-became-required",
            "request-enum-value-removed",
            "request-constraint-tightened",
        } <= rules

    def test_new_response_enum_value_is_only_a_warning(self) -> None:
        base = _plain(PETSTORE)
        new = copy.deepcopy(base)
        new["components"]["schemas"]["OrderStatus"]["enum"].append("SHIPPED")
        changes = [c for c in compare(base, new) if c.rule == "response-enum-value-added"]
        assert changes and not any(c.breaking for c in changes)

    def test_removed_success_status_and_new_required_parameter(self) -> None:
        base = _plain(PETSTORE)
        new = copy.deepcopy(base)
        post = new["paths"]["/orders"]["post"]
        post["responses"]["202"] = post["responses"].pop("201")
        new["paths"]["/orders"]["get"].setdefault("parameters", []).append(
            {"name": "region", "in": "query", "required": True, "schema": {"type": "string"}}
        )
        rules = _rules(compare(base, new))
        assert {"response-status-removed", "parameter-required-added"} <= rules

    def test_operation_id_rename_is_a_warning(self) -> None:
        base = _plain(PETSTORE)
        new = copy.deepcopy(base)
        new["paths"]["/orders"]["get"]["operationId"] = "findOrders"
        changes = compare(base, new)
        assert [(c.rule, c.breaking) for c in changes] == [("operation-id-changed", False)]


class TestBreakingInValidate:
    def _changed(self, tmp_path: Path) -> Path:
        doc = _plain(PETSTORE)
        del doc["paths"]["/orders/{orderId}"]["delete"]
        return _write(tmp_path / "openapi" / "orders.yaml", doc)

    def test_baseline_file_produces_o2j_v_011(
        self, cfg: O2JConfig, work: WorkDir, tmp_path: Path
    ) -> None:
        spec = self._changed(tmp_path)
        report = validate(cfg, spec, work, baseline=PETSTORE)
        breaking = [f for f in report.findings if f.id == "O2J-V-011"]
        assert [f.rule for f in breaking] == ["breaking-operation-removed"]
        assert breaking[0].severity == "error"  # production profile
        assert not report.gate.passed

    def test_severity_follows_profile_and_setting(
        self, cfg: O2JConfig, work: WorkDir, tmp_path: Path
    ) -> None:
        spec = self._changed(tmp_path)
        cfg.service.profile = "sandbox"
        report = validate(cfg, spec, work, baseline=PETSTORE)
        assert {f.severity for f in report.findings if f.id == "O2J-V-011"} == {"warn"}

        cfg.validation.breaking_change_severity = "error"
        report = validate(cfg, spec, work, baseline=PETSTORE)
        assert {f.severity for f in report.findings if f.id == "O2J-V-011"} == {"error"}

    def test_baseline_folder_is_matched_by_file_name(
        self, cfg: O2JConfig, work: WorkDir, tmp_path: Path
    ) -> None:
        spec = self._changed(tmp_path)
        released = tmp_path / "released"
        released.mkdir()
        shutil.copyfile(PETSTORE, released / "orders.yaml")

        report = validate(cfg, spec, work, baseline=released)
        assert any(f.rule == "breaking-operation-removed" for f in report.findings)

    def test_a_spec_with_no_baseline_is_new_not_broken(
        self, cfg: O2JConfig, work: WorkDir, tmp_path: Path
    ) -> None:
        spec = self._changed(tmp_path)
        empty = tmp_path / "released"
        empty.mkdir()
        report = validate(cfg, spec, work, baseline=empty)
        rules = {f.rule for f in report.findings if f.id == "O2J-V-011"}
        assert rules == {"breaking-change-baseline-missing"}

    def test_check_breaking_false_skips_the_comparison(
        self, cfg: O2JConfig, work: WorkDir, tmp_path: Path
    ) -> None:
        spec = self._changed(tmp_path)
        report = validate(cfg, spec, work, baseline=PETSTORE, check_breaking=False)
        assert not [f for f in report.findings if f.id == "O2J-V-011"]

    def test_configured_baseline_is_relative_to_o2j_yaml(self, tmp_path: Path) -> None:
        shutil.copyfile(REPO_ROOT / "o2j.yaml", tmp_path / "o2j.yaml")
        cfg = load_config(tmp_path / "o2j.yaml")
        (tmp_path / "released").mkdir()
        shutil.copyfile(PETSTORE, tmp_path / "released" / "orders.yaml")
        cfg.validation.breaking_change_baseline = "released"
        assert cfg.baseline_for("openapi/orders.yaml") == tmp_path / "released" / "orders.yaml"
        assert cfg.baseline_for("openapi/other.yaml") is None

    def test_breaking_rules_are_explained(self) -> None:
        doc = explain("breaking-response-type-widened")
        assert doc is not None and doc.rule == "breaking-change"


# ------------------------------------------------------------------ o2j lint


def _house_rules() -> set[str]:
    yaml = YAML(typ="safe")
    ruleset = yaml.load((REPO_ROOT / "o2j" / "rules" / "spectral" / "house-api.yaml").read_text())
    return set(ruleset["rules"])


class TestSpectralRulesetStaysInStep:
    def test_every_spectral_house_rule_has_a_python_equivalent(self) -> None:
        assert _house_rules() == set(lint_mod.SPECTRAL_EQUIVALENTS)

    def test_every_python_equivalent_exists(self) -> None:
        sources = "".join(
            p.read_text(encoding="utf-8") for p in (REPO_ROOT / "o2j" / "rules").glob("*.py")
        )
        missing = {
            rule
            for rule in lint_mod.SPECTRAL_EQUIVALENTS.values()
            if f'rule="{rule}"' not in sources
        }
        assert not missing, f"Spectral rules mapped to Python rules that do not exist: {missing}"

    def test_root_spectral_config_uses_the_house_ruleset(self) -> None:
        text = (REPO_ROOT / ".spectral.yaml").read_text(encoding="utf-8")
        assert "o2j/rules/spectral/house-api.yaml" in text


FAKE_SPECTRAL = """
import json, sys
spec = sys.argv[2]
print("npx: installed 1 package")  # noise before the JSON, as npx prints
print(json.dumps([
    {"code": "o2j-path-kebab-case", "path": ["paths", "/Orders"], "message": "kebab",
     "severity": 0, "range": {"start": {"line": 9, "character": 2}}, "source": spec},
    {"code": "info-contact", "path": ["info"], "message": "contact", "severity": 1,
     "range": {"start": {"line": 1, "character": 0}}, "source": spec},
]))
sys.exit(1)
"""


class TestLintCommand:
    def _project(self, tmp_path: Path) -> Path:
        shutil.copyfile(REPO_ROOT / "o2j.yaml", tmp_path / "o2j.yaml")
        (tmp_path / "openapi").mkdir()
        shutil.copyfile(PETSTORE, tmp_path / "openapi" / "orders.yaml")
        return tmp_path

    def test_spectral_results_become_findings_and_sarif(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = self._project(tmp_path)
        script = tmp_path / "fake_spectral.py"
        script.write_text(FAKE_SPECTRAL, encoding="utf-8")
        fake = ("spectral", [sys.executable, str(script)])
        monkeypatch.setattr(lint_mod, "resolve_tool", lambda cfg, tool="auto": fake)
        monkeypatch.chdir(project)

        result = CliRunner().invoke(app, ["lint", "--config", "o2j.yaml", "--json"])
        assert result.exit_code == 0, result.output  # advisory by default

        report = json.loads((project / "work" / "01-validate" / "lint-report.json").read_text())
        kebab = next(f for f in report["findings"] if f["rule"] == "o2j-path-kebab-case")
        assert kebab["severity"] == "error"
        assert kebab["location"]["line"] == 10  # Spectral lines are 0-based
        assert kebab["pointer"] == "#/paths/~1Orders"
        assert "path-kebab-case" in kebab["remediation"]
        sarif = json.loads((project / "work" / "01-validate" / "lint-report.sarif").read_text())
        assert sarif["runs"][0]["tool"]["driver"]["name"] == "o2j-lint (spectral)"

        strict = CliRunner().invoke(app, ["lint", "--config", "o2j.yaml", "--strict"])
        assert strict.exit_code == 3

    def test_missing_tool_is_reported_not_passed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = self._project(tmp_path)
        monkeypatch.setattr(lint_mod.shutil, "which", lambda name: None)
        monkeypatch.chdir(project)

        relaxed = CliRunner().invoke(app, ["lint", "--config", "o2j.yaml"])
        assert relaxed.exit_code == 0
        strict = CliRunner().invoke(app, ["lint", "--config", "o2j.yaml", "--strict"])
        assert strict.exit_code == 2
        report = json.loads((project / "work" / "01-validate" / "lint-report.json").read_text())
        assert report["status"] == "unavailable" and not report["gate"]["passed"]

    def test_ruleset_falls_back_to_the_packaged_one(self, tmp_path: Path) -> None:
        shutil.copyfile(REPO_ROOT / "o2j.yaml", tmp_path / "o2j.yaml")
        cfg = load_config(tmp_path / "o2j.yaml")
        assert lint_mod.ruleset_path(cfg) == lint_mod.PACKAGED_RULESET


# ------------------------------------------------------------------ Sonar


class TestSonar:
    def test_sonar_without_token_is_unavailable_not_passed(
        self, cfg: O2JConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg.quality.sonar.enabled = True
        cfg.quality.sonar.project_key = "acme_orders"
        cfg.quality.static_analysis.archunit = False
        cfg.quality.static_analysis.gitleaks = False
        monkeypatch.delenv("SONAR_TOKEN", raising=False)
        (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")

        report = quality_stage.quality(cfg, ["sonar"], root=tmp_path)

        sonar = [t for t in report.tools if t.tool == "sonar"]
        assert [t.status for t in sonar] == ["unavailable"]
        assert report.gate.passed  # unavailable is reported, but is not a failure

    def test_sonar_is_not_run_when_disabled(self, cfg: O2JConfig, tmp_path: Path) -> None:
        cfg.quality.static_analysis.archunit = False
        cfg.quality.static_analysis.gitleaks = False
        report = quality_stage.quality(cfg, ["sonar"], root=tmp_path)
        assert not [t for t in report.tools if t.tool == "sonar"]
