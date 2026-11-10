"""The contract gate: markers must name the operation, sit on a test, and that test must pass.

Each bypass tested here passed the gate before: a file of bare comments with one marker per
distinct case satisfied all 47 required cases of the 3.1 petstore without a single test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from o2j.config import O2JConfig
from o2j.models import GenerationPlan
from o2j.stages.normalize import normalize
from o2j.stages.plan import plan
from o2j.stages.verify import verify
from o2j.testmarkers import read_results, scan
from o2j.workdir import WorkDir
from tests.conftest import spec

PKG = "com.example.t"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "svc"
    (root / "src/test/java/com/example/t").mkdir(parents=True)
    (root / "target/surefire-reports").mkdir(parents=True)
    return root


@pytest.fixture
def petstore(cfg: O2JConfig, work: WorkDir) -> GenerationPlan:
    normalize(cfg, spec("01-petstore-3.0"), work=work)
    result = plan(cfg, work=work)
    work.write_json("plan", "generation-plan.json", result)
    return result


def _java(project: Path, name: str, body: str) -> None:
    (project / f"src/test/java/com/example/t/{name}.java").write_text(
        f"package {PKG};\n\n{body}\n", encoding="utf-8"
    )


def _report(project: Path, cls: str, cases: dict[str, str]) -> None:
    """Write a Surefire report. ``cases`` maps test name -> passed|failure|error|skipped."""
    rows = []
    for name, outcome in cases.items():
        inner = "" if outcome == "passed" else f"<{outcome}/>"
        rows.append(f'<testcase name="{name}" classname="{PKG}.{cls}">{inner}</testcase>')
    (project / f"target/surefire-reports/TEST-{PKG}.{cls}.xml").write_text(
        f'<testsuite name="{PKG}.{cls}">{"".join(rows)}</testsuite>', encoding="utf-8"
    )


def _all_markers_test(petstore: GenerationPlan) -> str:
    """One passing test method carrying every required marker, correctly scoped."""
    markers = "\n".join(
        f"  // {op.marker(case)}" for op in petstore.operations for case in op.required_tests
    )
    return f"class AllTest {{\n{markers}\n  @Test\n  void covers() {{}}\n}}"


class TestBypassesThatUsedToPass:
    def test_comment_only_file_fails(
        self, cfg: O2JConfig, work: WorkDir, project: Path, petstore: GenerationPlan
    ) -> None:
        lines = [f"// {op.marker(c)}" for op in petstore.operations for c in op.required_tests]
        _java(project, "Fake", "class Fake {}\n" + "\n".join(lines))
        _report(project, "Other", {"x": "passed"})
        report = verify(cfg, "contract", work, root=project)
        assert not report.passed
        assert any("is not on a test method" in d for d in report.differences)

    def test_unscoped_markers_do_not_count(
        self, cfg: O2JConfig, work: WorkDir, project: Path, petstore: GenerationPlan
    ) -> None:
        cases = sorted({c for op in petstore.operations for c in op.required_tests})
        markers = "\n".join(f"  // o2j:{c}" for c in cases)
        body = f"class OldStyleTest {{\n{markers}\n  @Test\n  void t() {{}}\n}}"
        _java(project, "OldStyleTest", body)
        _report(project, "OldStyleTest", {"t": "passed"})
        assert not verify(cfg, "contract", work, root=project).passed

    def test_marker_on_failing_test_fails(
        self, cfg: O2JConfig, work: WorkDir, project: Path, petstore: GenerationPlan
    ) -> None:
        _java(project, "AllTest", _all_markers_test(petstore))
        _report(project, "AllTest", {"covers": "failure"})
        report = verify(cfg, "contract", work, root=project)
        assert not report.passed
        assert any("AllTest#covers failed" in d for d in report.differences)

    def test_marker_on_test_that_did_not_run_fails(
        self, cfg: O2JConfig, work: WorkDir, project: Path, petstore: GenerationPlan
    ) -> None:
        _java(project, "AllTest", _all_markers_test(petstore))
        _report(project, "SomethingElse", {"x": "passed"})
        report = verify(cfg, "contract", work, root=project)
        assert any("did not run" in d for d in report.differences)

    def test_no_reports_fails_unless_static(
        self, cfg: O2JConfig, work: WorkDir, project: Path, petstore: GenerationPlan
    ) -> None:
        _java(project, "AllTest", _all_markers_test(petstore))
        assert not verify(cfg, "contract", work, root=project).passed
        assert verify(cfg, "contract", work, root=project, require_results=False).passed


class TestHonestSuite:
    def test_passing_scoped_markers_pass(
        self, cfg: O2JConfig, work: WorkDir, project: Path, petstore: GenerationPlan
    ) -> None:
        _java(project, "AllTest", _all_markers_test(petstore))
        _report(project, "AllTest", {"covers": "passed"})
        report = verify(cfg, "contract", work, root=project)
        assert report.passed, report.differences

    def test_one_passing_claim_is_enough(
        self, cfg: O2JConfig, work: WorkDir, project: Path, petstore: GenerationPlan
    ) -> None:
        """A case claimed by a failing and a passing test is covered by the passing one."""
        _java(project, "AllTest", _all_markers_test(petstore))
        _java(project, "AlsoTest", _all_markers_test(petstore).replace("AllTest", "AlsoTest"))
        _report(project, "AllTest", {"covers": "failure"})
        _report(project, "AlsoTest", {"covers": "passed"})
        assert verify(cfg, "contract", work, root=project).passed


class TestScanner:
    def test_marker_above_annotations_attaches_to_the_test(self, project: Path) -> None:
        _java(
            project,
            "ATest",
            'class ATest {\n  // o2j:op:unit:x\n  @DisplayName("a (b) {c}")\n  @Test\n'
            "  void first() {}\n}",
        )
        [marker] = scan(project / "src/test/java")
        assert marker.test is not None and marker.test.method == "first"

    def test_marker_inside_body_attaches(self, project: Path) -> None:
        _java(
            project, "ATest", "class ATest {\n  @Test\n  void t() {\n    // o2j:op:unit:x\n  }\n}"
        )
        [marker] = scan(project / "src/test/java")
        assert marker.test is not None and marker.test.method == "t"

    def test_marker_in_helper_method_is_unattached(self, project: Path) -> None:
        _java(
            project,
            "ATest",
            "class ATest {\n  @Test\n  void t() {}\n\n  void helper() {\n    // o2j:op:unit:x\n"
            "  }\n}",
        )
        [marker] = scan(project / "src/test/java")
        assert marker.test is None

    def test_code_between_marker_and_test_detaches_it(self, project: Path) -> None:
        _java(
            project,
            "ATest",
            "class ATest {\n  // o2j:op:unit:x\n  int field = 1;\n  @Test\n  void t() {}\n}",
        )
        [marker] = scan(project / "src/test/java")
        assert marker.test is None

    def test_nested_class_uses_binary_name(self, project: Path) -> None:
        _java(
            project,
            "ATest",
            "class ATest {\n  @Nested\n  class Inner {\n    // o2j:op:unit:x\n    @Test\n"
            "    void t() {}\n  }\n}",
        )
        [marker] = scan(project / "src/test/java")
        assert marker.test is not None
        assert marker.test.class_name == f"{PKG}.ATest$Inner"

    def test_braces_in_strings_do_not_confuse_the_scanner(self, project: Path) -> None:
        _java(
            project,
            "ATest",
            'class ATest {\n  @Test\n  void t() {\n    String s = "}}}";\n    // o2j:op:unit:x\n'
            "  }\n}",
        )
        [marker] = scan(project / "src/test/java")
        assert marker.test is not None and marker.test.method == "t"


class TestReports:
    def test_parameterized_invocations_fold_onto_the_method(self, project: Path) -> None:
        _report(project, "PTest", {"p(String)[1]": "passed", "p(String)[2]": "failure"})
        outcome = read_results([project / "target/surefire-reports"])[(f"{PKG}.PTest", "p")]
        assert not outcome.passed

    def test_skipped_does_not_pass(self, project: Path) -> None:
        _report(project, "STest", {"s": "skipped"})
        outcome = read_results([project / "target/surefire-reports"])[(f"{PKG}.STest", "s")]
        assert outcome.status == "skipped" and not outcome.passed
