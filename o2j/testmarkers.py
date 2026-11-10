"""Find the test method each ``// o2j:`` marker belongs to, and whether that test passed.

The contract gate used to accept a marker anywhere in any ``.java`` file, and markers were not
tied to an operation. A single file of comments, with no test in it, satisfied every required
case of every operation. A marker now counts only when it

* names the operation: ``// o2j:<operationId>:<case>``,
* is attached to a JUnit test method (inside its body, or directly above it with only
  annotations and comments in between), and
* that test ran and passed, according to the Surefire/Failsafe XML reports.

This is a scanner, not a Java parser. Comments, strings, char literals and text blocks are
blanked before braces and declarations are matched, which is what the heuristics need.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

MARKER = re.compile(r"o2j:([A-Za-z0-9_.\-]+:[a-z]+:[A-Za-z0-9\-:_.]+)")
TEST_ANNOTATION = re.compile(r"@(?:Test|ParameterizedTest|RepeatedTest|TestTemplate)\b")
TEST_DECL = re.compile(r"\bvoid\s+(\w+)\s*\(")
TYPE_DECL = re.compile(r"\b(?:class|record|enum|interface)\s+(\w+)")
PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)
ONLY_ANNOTATIONS = re.compile(r"^(?:\s*@[\w.]+(?:\s*\([^()]*(?:\([^()]*\)[^()]*)*\))?)*\s*$")


@dataclass(frozen=True)
class TestMethod:
    class_name: str  # binary name, nested classes joined with '$'
    method: str
    file: str


@dataclass(frozen=True)
class Marker:
    claim: str  # "<operationId>:<layer>:<case>"
    file: str
    line: int
    test: TestMethod | None  # None when not attached to a test method


@dataclass(frozen=True)
class TestOutcome:
    passed: bool
    status: str  # passed | failed | error | skipped


def scan(test_root: Path) -> list[Marker]:
    """Every marker under ``test_root`` with the test method it is attached to."""
    markers: list[Marker] = []
    if not test_root.is_dir():
        return markers
    for path in sorted(test_root.rglob("*.java")):
        source = path.read_text(encoding="utf-8", errors="replace")
        markers.extend(_scan_file(source, str(path)))
    return markers


def _scan_file(source: str, file: str) -> list[Marker]:
    found = list(MARKER.finditer(source))
    if not found:
        return []

    code = _blank_non_code(source)
    package_match = PACKAGE.search(code)
    package = package_match.group(1) if package_match else ""
    types = _type_spans(code)
    tests = _test_methods(code, types, package, file)

    out: list[Marker] = []
    for match in found:
        position = match.start()
        line = source.count("\n", 0, position) + 1
        out.append(
            Marker(
                claim=match.group(1),
                file=file,
                line=line,
                test=_attach(code, position, tests),
            )
        )
    return out


@dataclass(frozen=True)
class _Span:
    name: str
    start: int  # position of the opening brace
    end: int  # position of the matching closing brace


@dataclass(frozen=True)
class _Test:
    annotation: int
    body: _Span
    method: TestMethod


def _test_methods(code: str, types: list[_Span], package: str, file: str) -> list[_Test]:
    tests: list[_Test] = []
    for annotation in TEST_ANNOTATION.finditer(code):
        decl = TEST_DECL.search(code, annotation.end())
        if decl is None:
            continue
        brace = code.find("{", decl.end())
        if brace < 0:
            continue
        end = _matching_brace(code, brace)
        enclosing = [t for t in types if t.start < annotation.start() < t.end]
        if not enclosing:
            continue
        nested = "$".join(t.name for t in sorted(enclosing, key=lambda t: t.start))
        class_name = f"{package}.{nested}" if package else nested
        tests.append(
            _Test(
                annotation=annotation.start(),
                body=_Span(decl.group(1), brace, end),
                method=TestMethod(class_name, decl.group(1), file),
            )
        )
    return tests


def _attach(code: str, position: int, tests: list[_Test]) -> TestMethod | None:
    for test in tests:
        # Inside the body, or between the annotation and the body.
        if test.annotation <= position <= test.body.end:
            return test.method
    # Directly above: only annotations, comments and whitespace before the test annotation.
    # The marker is a comment, so it is blank in `code`; start after its line.
    line_end = code.find("\n", position)
    line_end = len(code) if line_end < 0 else line_end
    following = [t for t in tests if t.annotation > position]
    if following:
        nearest = min(following, key=lambda t: t.annotation)
        if ONLY_ANNOTATIONS.match(code[line_end : nearest.annotation]):
            return nearest.method
    return None


def _type_spans(code: str) -> list[_Span]:
    spans: list[_Span] = []
    for decl in TYPE_DECL.finditer(code):
        brace = code.find("{", decl.end())
        if brace < 0:
            continue
        spans.append(_Span(decl.group(1), brace, _matching_brace(code, brace)))
    return spans


def _matching_brace(code: str, open_at: int) -> int:
    depth = 0
    for i in range(open_at, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return len(code)


def _blank_non_code(source: str) -> str:
    """Replace comments and literals with spaces, preserving length and newlines."""
    out = list(source)
    i, n = 0, len(source)

    def blank(start: int, end: int) -> None:
        for k in range(start, min(end, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        if source.startswith("//", i):
            end = source.find("\n", i)
            end = n if end < 0 else end
            blank(i, end)
            i = end
        elif source.startswith("/*", i):
            end = source.find("*/", i + 2)
            end = n if end < 0 else end + 2
            blank(i, end)
            i = end
        elif source.startswith('"""', i):
            end = source.find('"""', i + 3)
            end = n if end < 0 else end + 3
            blank(i + 1, end - 1)
            i = end
        elif source[i] in "\"'":
            quote, j = source[i], i + 1
            while j < n and source[j] != quote and source[j] != "\n":
                j += 2 if source[j] == "\\" else 1
            blank(i + 1, j)
            i = j + 1
        else:
            i += 1
    return "".join(out)


def read_results(report_dirs: list[Path]) -> dict[tuple[str, str], TestOutcome]:
    """Test outcomes from Surefire/Failsafe XML, keyed by (class name, method name).

    Parameterized and repeated tests report ``method(args)[n]`` or ``method()[n]``; they are
    folded onto the method, which passes only if every invocation passed.
    """
    results: dict[tuple[str, str], TestOutcome] = {}
    for directory in report_dirs:
        if not directory.is_dir():
            continue
        for report in sorted(directory.glob("TEST-*.xml")):
            try:
                root = ET.parse(report).getroot()  # noqa: S314 - our own build output
            except ET.ParseError:
                continue
            for case in root.iter("testcase"):
                class_name = case.get("classname", "")
                method = re.split(r"[(\[]", case.get("name", ""), maxsplit=1)[0]
                status = "passed"
                for child, label in (("failure", "failed"), ("error", "error")):
                    if case.find(child) is not None:
                        status = label
                if status == "passed" and case.find("skipped") is not None:
                    status = "skipped"
                key = (class_name, method)
                previous = results.get(key)
                if previous is not None and not previous.passed:
                    continue
                results[key] = TestOutcome(passed=status == "passed", status=status)
    return results
