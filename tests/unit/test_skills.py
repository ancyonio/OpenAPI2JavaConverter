"""Lint the Agent Skills in `skills/`, and their runtime mirrors.

A malformed skill is not a loud failure: the runtime skips it, and the agent that needed it
simply works without it and produces worse output. Same failure class as a typo in an agent's
`tools` list, so it gets the same treatment - a test.

Constraints are from the Agent Skills specification (agentskills.io/specification) and
GitHub's Copilot skills documentation, which agree except where noted.

Portability is checked too, because a skill that only works inside this repository is not
reusable, and reuse is the entire reason these exist as skills rather than as agent text.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from o2j.stages import skills as skills_stage

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SKILLS_DIR = REPO_ROOT / "skills"
AGENTS_DIR = REPO_ROOT / ".github" / "agents"

# Spec: 1-64 chars, lowercase alphanumeric and hyphens, no leading/trailing hyphen,
# no consecutive hyphens, must match the parent directory name.
NAME_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
NAME_MAX = 64
DESCRIPTION_MAX = 1024
COMPATIBILITY_MAX = 500
BODY_MAX_LINES = 500

# Spec frontmatter. Unknown fields are ignored by other runtimes, so a portable skill sticks
# to these - an extra field is silently dropped rather than reported.
VALID_FRONTMATTER_KEYS: frozenset[str] = frozenset(
    {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
)

EXPECTED_SKILLS = {
    # Contract and generation
    "openapi-contract-review",
    "openapi-spec-analysis",
    "openapi-version-migration",
    "openapi-to-java-mapping",
    "openapi-codegen-determinism",
    # Implementation
    "spring-boot-rest-service",
    "spring-validation-error-handling",
    "spring-security-openapi",
    "jpa-persistence-design",
    "service-resilience-observability",
    # Verification
    "junit-mockito-test-design",
    "rest-api-testing",
    "java-code-quality-gates",
    # Maintenance
    "java-spring-upgrade",
}

# Which agent must point at which skill. This is the anti-drift check: the knowledge lives in
# one place, and the agent is expected to say where.
AGENT_SKILL_MAP: dict[str, str] = {
    "yaml-validator": "openapi-contract-review",
    "spec-normalizer": "openapi-version-migration",
    "api-generator": "openapi-codegen-determinism",
    "service-implementer": "spring-boot-rest-service",
    "test-author": "junit-mockito-test-design",
    "quality-reviewer": "spring-boot-rest-service",
    "migration-advisor": "java-spring-upgrade",
}

MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
BACKTICK_PATH = re.compile(r"`(references/[A-Za-z0-9_.\-/]+)`")


def skill_dirs() -> list[Path]:
    if not SKILLS_DIR.is_dir():
        return []
    return sorted(p for p in SKILLS_DIR.iterdir() if p.is_dir())


def split_frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise AssertionError(f"{path} has no YAML frontmatter")
    _, raw, body = text.split("---", 2)
    data = YAML(typ="safe").load(raw) or {}
    return data, body


class TestInventory:
    def test_all_expected_skills_exist(self) -> None:
        assert {p.name for p in skill_dirs()} == EXPECTED_SKILLS

    def test_each_skill_has_a_skill_md(self) -> None:
        for directory in skill_dirs():
            assert (directory / "SKILL.md").is_file(), f"{directory.name} has no SKILL.md"

    def test_filename_casing_is_exact(self) -> None:
        """The runtime looks for `SKILL.md` exactly; `skill.md` is not documented as accepted."""
        for directory in skill_dirs():
            names = {p.name for p in directory.iterdir() if p.is_file()}
            wrong = {n for n in names if n.lower() == "skill.md" and n != "SKILL.md"}
            assert not wrong, f"{directory.name} has wrongly-cased {wrong}"

    def test_skill_directories_are_one_level_deep(self) -> None:
        """Nesting is undocumented, so a skill under a group directory may never load."""
        for directory in skill_dirs():
            nested = [
                p for p in directory.rglob("SKILL.md") if p.parent != directory
            ]
            assert not nested, f"{directory.name} contains a nested SKILL.md: {nested}"


@pytest.mark.parametrize("directory", skill_dirs(), ids=lambda p: p.name)
class TestSkillFrontmatter:
    def test_name_is_present_and_valid(self, directory: Path) -> None:
        data, _ = split_frontmatter(directory / "SKILL.md")
        name = data.get("name")
        assert name, f"{directory.name}: `name` is required"
        assert len(name) <= NAME_MAX, f"{name!r} exceeds {NAME_MAX} characters"
        assert NAME_PATTERN.match(name), (
            f"{name!r} must be lowercase alphanumeric with single hyphens, "
            f"no leading or trailing hyphen"
        )
        assert "--" not in name, f"{name!r} contains consecutive hyphens"

    def test_name_matches_directory(self, directory: Path) -> None:
        """The spec requires this; GitHub only says "typically". Hold the stricter rule."""
        data, _ = split_frontmatter(directory / "SKILL.md")
        assert data.get("name") == directory.name, (
            f"name {data.get('name')!r} does not match directory {directory.name!r}"
        )

    def test_description_is_present_and_within_limit(self, directory: Path) -> None:
        data, _ = split_frontmatter(directory / "SKILL.md")
        description = data.get("description")
        assert description, f"{directory.name}: `description` is required"
        assert len(description) <= DESCRIPTION_MAX, (
            f"{directory.name}: description is {len(description)} chars "
            f"(limit {DESCRIPTION_MAX})"
        )

    def test_description_says_when_to_use(self, directory: Path) -> None:
        """The description is the only thing loaded at startup, so it is what decides whether
        the skill is ever consulted. One that describes only *what* the skill does, without
        the situations that call for it, under-triggers."""
        data, _ = split_frontmatter(directory / "SKILL.md")
        description = str(data["description"]).lower()
        assert "use" in description, (
            f"{directory.name}: description does not say when to use the skill"
        )
        assert len(description) > 200, (
            f"{directory.name}: description is too thin to route on reliably"
        )

    def test_frontmatter_keys_are_portable(self, directory: Path) -> None:
        data, _ = split_frontmatter(directory / "SKILL.md")
        unknown = set(data) - VALID_FRONTMATTER_KEYS
        assert not unknown, (
            f"{directory.name}: non-standard frontmatter {sorted(unknown)} is silently "
            f"ignored by other runtimes"
        )

    def test_compatibility_within_limit(self, directory: Path) -> None:
        data, _ = split_frontmatter(directory / "SKILL.md")
        compatibility = data.get("compatibility")
        if compatibility is None:
            return
        assert len(compatibility) <= COMPATIBILITY_MAX, (
            f"{directory.name}: compatibility is {len(compatibility)} chars "
            f"(limit {COMPATIBILITY_MAX})"
        )

    def test_body_within_recommended_length(self, directory: Path) -> None:
        """Over ~500 lines the body crowds the context it was meant to inform; move detail
        into reference files, which load only when they are actually needed."""
        _, body = split_frontmatter(directory / "SKILL.md")
        lines = len(body.splitlines())
        assert lines <= BODY_MAX_LINES, (
            f"{directory.name}: SKILL.md body is {lines} lines (recommended max "
            f"{BODY_MAX_LINES}); move detail into references/"
        )

    def test_body_is_substantive(self, directory: Path) -> None:
        _, body = split_frontmatter(directory / "SKILL.md")
        assert len(body.strip()) > 1000, f"{directory.name}: body has little content"


@pytest.mark.parametrize("directory", skill_dirs(), ids=lambda p: p.name)
class TestReferences:
    def test_referenced_files_exist(self, directory: Path) -> None:
        """A dangling reference costs the reader a failed lookup at the moment they needed it."""
        skill_md = directory / "SKILL.md"
        body = skill_md.read_text(encoding="utf-8")

        targets = set(MARKDOWN_LINK.findall(body)) | set(BACKTICK_PATH.findall(body))
        for target in targets:
            if target.startswith(("http://", "https://", "#")):
                continue
            if not target.startswith("references/"):
                continue
            assert (directory / target).is_file(), (
                f"{directory.name}: SKILL.md references missing file {target}"
            )

    def test_reference_files_are_one_level_deep(self, directory: Path) -> None:
        """The spec asks for this so a reader does not have to follow a chain of files."""
        references = directory / "references"
        if not references.is_dir():
            return
        deep = [p for p in references.rglob("*") if p.is_file() and p.parent != references]
        assert not deep, f"{directory.name}: reference files nested too deep: {deep}"

    def test_every_reference_file_is_linked(self, directory: Path) -> None:
        """An unreferenced file never loads, so it is dead weight the author believes is live."""
        references = directory / "references"
        if not references.is_dir():
            return
        body = (directory / "SKILL.md").read_text(encoding="utf-8")
        for path in sorted(references.glob("*")):
            if path.is_file():
                assert path.name in body, (
                    f"{directory.name}: references/{path.name} is never linked from SKILL.md"
                )


@pytest.mark.parametrize("directory", skill_dirs(), ids=lambda p: p.name)
class TestPortability:
    """A skill that only works in this repository is not reusable, which defeats the point."""

    def test_no_absolute_or_windows_paths(self, directory: Path) -> None:
        for path in sorted(directory.rglob("*.md")):
            text = path.read_text(encoding="utf-8")
            assert "D:\\" not in text and "C:\\" not in text, (
                f"{path}: contains an absolute Windows path"
            )
            assert not re.search(r"(?<![\w`/.])/(home|Users)/", text), (
                f"{path}: contains an absolute POSIX home path"
            )

    def test_no_dependency_on_this_repositorys_layout(self, directory: Path) -> None:
        """Naming this project's own files would break the skill when copied elsewhere.

        `o2j` may appear as an example tool and the marker prefix, but a skill must not require
        this repo's config or module layout to be present.
        """
        forbidden = ("o2j.yaml", "o2j/stages/", "o2j/rules/", "tests/corpus/", "SPECIFICATION.md")
        for path in sorted(directory.rglob("*.md")):
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                assert token not in text, (
                    f"{path}: references {token!r}, which will not exist in another repository"
                )


class TestAgentsReferenceSkills:
    """The anti-drift check.

    Knowledge is held once, in the skill, and the agent points at it. If an agent stops
    pointing, the two copies start diverging and the one that is wrong is whichever the reader
    did not open.
    """

    @pytest.mark.parametrize(
        ("agent", "skill"), sorted(AGENT_SKILL_MAP.items()), ids=lambda v: str(v)
    )
    def test_agent_points_at_its_skill(self, agent: str, skill: str) -> None:
        path = AGENTS_DIR / f"{agent}.agent.md"
        assert path.is_file(), f"missing agent {path}"
        body = path.read_text(encoding="utf-8")
        assert f"skills/{skill}/SKILL.md" in body, (
            f"{agent} does not point at the {skill} skill, so its knowledge will drift"
        )

    def test_agents_do_not_restate_skill_content_at_length(self) -> None:
        """An agent should hold procedure and scope, not a second copy of the domain rules.

        Length is a crude proxy, but a good one here: the agents that grew past this were the
        ones duplicating a skill, and trimming them is what the skills are for.
        """
        oversized: list[str] = []
        for agent in sorted(AGENT_SKILL_MAP):
            body = (AGENTS_DIR / f"{agent}.agent.md").read_text(encoding="utf-8")
            if len(body.splitlines()) > 120:
                oversized.append(f"{agent} ({len(body.splitlines())} lines)")
        assert not oversized, (
            f"these agents are long enough to be duplicating their skill: {oversized}"
        )


class TestRuntimeMirrors:
    """`skills/` is canonical, but no runtime reads it directly.

    This is a GitHub Copilot solution, so the canonical copy is mirrored into
    `.github/skills` - Copilot's native location. These tests make a stale mirror a build
    failure rather than a silent divergence, where Copilot would serve old guidance while the
    canonical copy has moved on.

    GitHub also documents `.claude/skills` and `.agents/skills` as Copilot locations, and
    `.claude/skills` is the only one Claude Code reads; both are one line in
    `skills_stage.MIRRORS` should another runtime need these.
    """

    def test_every_runtime_location_exists(self) -> None:
        for mirror in skills_stage.MIRRORS:
            path = REPO_ROOT / mirror
            assert path.is_dir(), (
                f"{mirror} is missing; run `o2j skills sync` so that runtime can see the skills"
            )

    def test_mirrors_are_byte_identical_to_canonical(self) -> None:
        result = skills_stage.check(REPO_ROOT)
        assert result.in_sync, (
            "runtime mirrors have drifted from skills/ - run `o2j skills sync`:\n  "
            + "\n  ".join(result.differences)
        )

    def test_sync_is_idempotent(self) -> None:
        """A sync that keeps rewriting files would churn the working tree on every run."""
        first = skills_stage.sync(REPO_ROOT)
        assert first.files_written == 0, (
            f"sync rewrote {first.files_written} file(s) when already in sync"
        )
        assert not first.removed

    def test_canonical_is_not_one_of_the_mirrors(self) -> None:
        """If the canonical directory were also a mirror target, sync would copy onto itself."""
        assert skills_stage.CANONICAL not in skills_stage.MIRRORS


class TestTriggerEvalSets:
    """Keep the trigger eval sets honest.

    These sets are the specification for *when* each skill should be consulted, and they are
    what any description tuning is measured against. A set with a conflict in it, or with
    negatives that are all obviously irrelevant, measures nothing - a skill passes while still
    mis-routing in practice.
    """

    EVALS = REPO_ROOT / "tests" / "trigger-evals"

    def _sets(self) -> dict[str, list[dict]]:
        import json

        return {
            p.stem: json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(self.EVALS.glob("*.json"))
            if not p.stem.startswith("_")
        }

    def test_every_skill_has_an_eval_set(self) -> None:
        assert set(self._sets()) == EXPECTED_SKILLS

    def test_each_set_is_balanced(self) -> None:
        for skill, data in self._sets().items():
            positives = sum(1 for item in data if item["should_trigger"])
            negatives = len(data) - positives
            assert positives >= 8, f"{skill}: only {positives} positive queries"
            assert negatives >= 8, f"{skill}: only {negatives} negative queries"

    def test_no_duplicate_queries_within_a_set(self) -> None:
        for skill, data in self._sets().items():
            queries = [item["query"].strip() for item in data]
            duplicates = {q for q in queries if queries.count(q) > 1}
            assert not duplicates, f"{skill}: duplicate queries {duplicates}"

    def test_no_two_skills_claim_the_same_query(self) -> None:
        """A query positive for two skills has no single right answer, so it cannot be scored."""
        owners: dict[str, str] = {}
        for skill, data in self._sets().items():
            for item in data:
                if not item["should_trigger"]:
                    continue
                query = item["query"].strip()
                assert query not in owners, (
                    f"{query!r} is claimed by both {owners[query]} and {skill}"
                )
                owners[query] = skill

    def test_queries_are_substantive(self) -> None:
        """A terse query like 'format this' tests nothing: routing needs real context."""
        for skill, data in self._sets().items():
            short = [item["query"] for item in data if len(item["query"]) < 40]
            assert not short, f"{skill}: queries too thin to route on: {short}"

    def test_negatives_include_cross_skill_near_misses(self) -> None:
        """The valuable negatives are a neighbour's work, not unrelated noise.

        A set whose negatives are all from unrelated domains would pass trivially while the
        skill still steals requests from the skill next to it.
        """
        import json

        routing_path = self.EVALS / "_routing.json"
        assert routing_path.is_file(), "run tests/trigger-evals/build_routing.py"
        routing = json.loads(routing_path.read_text(encoding="utf-8"))

        owned = sum(1 for item in routing if item["expected"] != "none")
        unowned = len(routing) - owned
        assert owned >= 60, f"only {owned} queries have an expected owner"
        assert unowned >= 20, f"only {unowned} queries are expected to route nowhere"

    def test_negatives_are_not_paraphrases_of_a_positive(self) -> None:
        """A near-copy of another skill's positive must be an exact copy, or `none` wins.

        `build_routing.py` resolves a cross-skill negative by matching the owner's exact query
        text. A *paraphrase* matches nothing, falls through to "none", and then scores the
        correct answer as wrong. This cost two false conclusions during development, so it is
        a test rather than a note.

        The fix when this fails is to make the negative byte-identical to the owner's positive,
        not to reword it further.
        """
        import difflib

        sets = self._sets()
        positives = {
            item["query"].strip()
            for data in sets.values()
            for item in data
            if item["should_trigger"]
        }
        negatives = {
            item["query"].strip()
            for data in sets.values()
            for item in data
            if not item["should_trigger"]
        }

        # A paraphrase is fine when CROSS_SKILL already maps it to its owner. Only flag the
        # ones that fall through to "none", because those are what score a correct answer wrong.
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "build_routing", self.EVALS / "build_routing.py"
        )
        assert spec and spec.loader
        build_routing = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build_routing)

        suspicious: list[str] = []
        for negative in sorted(negatives - positives):
            if build_routing.owner_of_negative(negative) != "none":
                continue  # already resolved to its real owner
            match = difflib.get_close_matches(negative, positives, n=1, cutoff=0.80)
            if match:
                suspicious.append(f"{negative[:60]!r} ~ {match[0][:60]!r}")

        assert not suspicious, (
            "these negatives fall through to 'none' but look like paraphrases of a "
            "positive; make them exact copies so the owner resolves, or reword them to "
            "be genuinely unrelated -- " + " | ".join(suspicious)
        )

    def test_routing_ground_truth_is_current(self) -> None:
        """The checked-in ground truth must match what the builder produces now.

        Otherwise a new eval set silently is not measured, and a score covers fewer queries
        than it appears to.
        """
        import subprocess
        import sys

        before = (self.EVALS / "_routing.json").read_text(encoding="utf-8")
        subprocess.run(
            [sys.executable, "-I", str(self.EVALS / "build_routing.py")],
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
        )
        after = (self.EVALS / "_routing.json").read_text(encoding="utf-8")
        assert before == after, (
            "_routing.json is stale; run `python -I tests/trigger-evals/build_routing.py`"
        )
