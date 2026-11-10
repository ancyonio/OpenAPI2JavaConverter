"""Rule coverage against the golden corpus.

These tests assert *which* findings appear, not how many. A count assertion passes when one
rule breaks and another over-reports, which is exactly the regression worth catching.
"""

from __future__ import annotations

import pytest

from o2j.config import O2JConfig
from o2j.rulebook import RULEBOOK, explain
from o2j.stages.validate import validate
from tests.conftest import spec


def rules_of(cfg: O2JConfig, fixture: str) -> set[str]:
    report = validate(cfg, spec(fixture))
    return {f.rule for f in report.findings}


class TestCleanFixtures:
    def test_minimal_spec_passes_the_gate(self, cfg: O2JConfig) -> None:
        report = validate(cfg, spec("00-minimal"))
        assert report.gate.passed, [f.message for f in report.findings if f.severity == "error"]

    def test_reference_31_spec_passes_the_gate(self, cfg: O2JConfig) -> None:
        report = validate(cfg, spec("02-petstore-3.1"))
        assert report.gate.passed, [f.message for f in report.findings if f.severity == "error"]

    def test_30_forms_are_not_reported_in_a_30_document(self, cfg: O2JConfig) -> None:
        """`nullable: true` and boolean `exclusiveMinimum` are correct in 3.0.

        Reporting them here would be a false positive, and a false positive in a blocking gate
        trains people to ignore the gate.
        """
        rules = rules_of(cfg, "01-petstore-3.0")
        assert "nullable-representation-mismatch" not in rules
        assert "exclusive-bound-form-mismatch" not in rules


class TestPathologicalFixture:
    """Every trap in 05-pathological must be caught."""

    @pytest.fixture
    def rules(self, cfg: O2JConfig) -> set[str]:
        return rules_of(cfg, "05-pathological")

    def test_gate_fails(self, cfg: O2JConfig) -> None:
        report = validate(cfg, spec("05-pathological"))
        assert not report.gate.passed
        assert report.summary.errors > 0

    @pytest.mark.parametrize(
        "rule",
        [
            "operation-id-required",
            "operation-id-duplicate",
            "operation-id-camel-case",
            "nullable-representation-mismatch",
            "exclusive-bound-form-mismatch",
            "ref-unresolvable",
            "ref-with-siblings",
            "response-schema-required",
            "inline-schema-not-allowed",
            "allof-depth-exceeded",
            "path-kebab-case",
            "no-verbs-in-path",
            "error-responses-declared",
            "problem-details-media-type",
            "security-required",
        ],
    )
    def test_rule_fires(self, rules: set[str], rule: str) -> None:
        assert rule in rules, f"{rule} did not fire on the pathological fixture"

    def test_injection_in_description_is_detected(self, cfg: O2JConfig) -> None:
        report = validate(cfg, spec("05-pathological"))
        injection = [f for f in report.findings if f.id == "O2J-V-013"]
        assert injection, "agent-directed text in a description was not detected"
        assert all(f.pointer.startswith("#/") for f in injection)


class TestFindingQuality:
    """A finding that cannot be acted on is a finding that gets ignored."""

    def test_every_finding_has_a_pointer(self, cfg: O2JConfig) -> None:
        report = validate(cfg, spec("05-pathological"))
        assert all(f.pointer for f in report.findings)

    def test_every_error_has_a_remediation(self, cfg: O2JConfig) -> None:
        report = validate(cfg, spec("05-pathological"))
        missing = [f.rule for f in report.findings if f.severity == "error" and not f.remediation]
        assert not missing, f"errors without a remediation: {sorted(set(missing))}"

    def test_sarif_is_emitted_for_every_finding(self, cfg: O2JConfig) -> None:
        from o2j.stages.validate import to_sarif

        report = validate(cfg, spec("05-pathological"))
        sarif = to_sarif(report, "spec.yaml")
        results = sarif["runs"][0]["results"]  # type: ignore[index]
        assert len(results) == len(report.findings)


class TestRulebook:
    def test_every_fired_rule_has_a_rulebook_entry(self, cfg: O2JConfig) -> None:
        """An agent asked to fix a finding should be able to look up why it exists."""
        fired: set[str] = set()
        for fixture in ("05-pathological", "02-petstore-3.1", "01-petstore-3.0"):
            fired |= rules_of(cfg, fixture)

        undocumented = {r for r in fired if explain(r) is None}
        assert not undocumented, f"rules with no rulebook entry: {sorted(undocumented)}"

    def test_rulebook_entries_explain_consequences(self) -> None:
        thin = [doc.rule for doc in RULEBOOK.values() if len(doc.why) < 80]
        assert not thin, f"rulebook entries too thin to be useful: {thin}"


class TestTargetTier:
    def test_non_lts_java_in_production_is_an_error(self, cfg: O2JConfig) -> None:
        cfg.target.java_version = 23
        cfg.service.profile = "production"
        report = validate(cfg, spec("00-minimal"))
        assert "JAVA_TIER2_IN_PRODUCTION" in {f.rule for f in report.findings}
        assert not report.gate.passed

    def test_non_lts_java_in_sandbox_is_allowed(self, cfg: O2JConfig) -> None:
        cfg.target.java_version = 23
        cfg.service.profile = "sandbox"
        report = validate(cfg, spec("00-minimal"))
        assert "JAVA_TIER2_IN_PRODUCTION" not in {f.rule for f in report.findings}

    @pytest.mark.parametrize("version", [17, 21, 25])
    def test_lts_versions_are_accepted_in_production(self, cfg: O2JConfig, version: int) -> None:
        cfg.target.java_version = version
        cfg.service.profile = "production"
        report = validate(cfg, spec("00-minimal"))
        assert report.gate.passed
