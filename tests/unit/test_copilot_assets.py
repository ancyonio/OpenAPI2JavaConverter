"""Lint the Copilot customization layer.

Copilot *silently ignores* an unrecognised tool name in an agent's `tools` list. A typo
therefore does not fail loudly - it quietly removes a capability, and the agent then explains
that it cannot do something it was designed to do. These tests exist because that failure mode
is invisible at runtime.

Schema per GitHub's custom-agents configuration reference.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
AGENTS_DIR = REPO_ROOT / ".github" / "agents"
INSTRUCTIONS_DIR = REPO_ROOT / ".github" / "instructions"
PROMPTS_DIR = REPO_ROOT / ".github" / "prompts"

# Primary aliases and their documented compatible forms.
VALID_TOOLS: frozenset[str] = frozenset(
    {
        "execute", "shell", "Bash", "powershell",
        "read", "Read", "NotebookRead",
        "edit", "Edit", "MultiEdit", "Write", "NotebookEdit",
        "search", "Grep", "Glob",
        "agent", "custom-agent", "Task",
        "web", "WebSearch", "WebFetch",
        "todo", "TodoWrite",
        "*",
    }
)

VALID_FRONTMATTER_KEYS: frozenset[str] = frozenset(
    {
        "name",
        "description",
        "target",
        "tools",
        "model",
        "disable-model-invocation",
        "user-invocable",
        "mcp-servers",
        "metadata",
    }
)

BODY_LIMIT = 30_000

EXPECTED_AGENTS = {
    "openapi-to-spring",
    "yaml-validator",
    "spec-normalizer",
    "api-generator",
    "service-implementer",
    "test-author",
    "quality-reviewer",
    "migration-advisor",
}


def split_frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise AssertionError(f"{path.name} has no YAML frontmatter")
    _, raw, body = text.split("---", 2)
    data = YAML(typ="safe").load(raw) or {}
    return data, body


def agent_files() -> list[Path]:
    return sorted(AGENTS_DIR.glob("*.agent.md"))


class TestAgentInventory:
    def test_all_specified_agents_exist(self) -> None:
        found = {p.name.removesuffix(".agent.md") for p in agent_files()}
        assert found == EXPECTED_AGENTS

    def test_agents_use_the_agent_md_extension(self) -> None:
        """GitHub resolves `.agent.md`; a plain `.md` file in this directory is inert."""
        stray = [p.name for p in AGENTS_DIR.glob("*.md") if not p.name.endswith(".agent.md")]
        assert not stray, f"files that will not be loaded as agents: {stray}"


@pytest.mark.parametrize("path", agent_files(), ids=lambda p: p.name)
class TestAgentFile:
    def test_description_is_present_and_substantive(self, path: Path) -> None:
        """`description` is required and drives automatic agent selection."""
        data, _ = split_frontmatter(path)
        description = data.get("description")
        assert description, f"{path.name} has no description"
        assert len(str(description)) > 80, f"{path.name} description is too thin to route on"

    def test_frontmatter_keys_are_recognised(self, path: Path) -> None:
        data, _ = split_frontmatter(path)
        unknown = set(data) - VALID_FRONTMATTER_KEYS
        assert not unknown, f"{path.name} has unrecognised frontmatter keys: {sorted(unknown)}"

    def test_tool_names_are_valid(self, path: Path) -> None:
        """An unrecognised tool name is silently dropped, removing a capability."""
        data, _ = split_frontmatter(path)
        tools = data.get("tools")
        if tools is None:
            return  # omitting `tools` enables all tools, which is legal
        if isinstance(tools, str):
            tools = [t.strip() for t in tools.split(",") if t.strip()]

        for tool in tools:
            if "/" in tool:
                continue  # MCP form: server/tool or server/*
            assert tool in VALID_TOOLS, f"{path.name}: unknown tool alias {tool!r}"

    def test_body_is_within_the_limit(self, path: Path) -> None:
        _, body = split_frontmatter(path)
        assert len(body) <= BODY_LIMIT, (
            f"{path.name} body is {len(body)} chars (limit {BODY_LIMIT})"
        )

    def test_body_is_not_empty(self, path: Path) -> None:
        _, body = split_frontmatter(path)
        assert len(body.strip()) > 400, f"{path.name} has no substantive instructions"


class TestLeastPrivilege:
    """Tool lists are the enforcement mechanism for separation of duties."""

    def test_validator_cannot_edit_source(self) -> None:
        data, body = split_frontmatter(AGENTS_DIR / "yaml-validator.agent.md")
        assert "edit" in data["tools"]
        assert "must not edit anything under `generated_Code/`" in body

    def test_normalizer_has_no_edit_tool(self) -> None:
        """Normalization is deterministic; the agent reviews it rather than performing it."""
        data, _ = split_frontmatter(AGENTS_DIR / "spec-normalizer.agent.md")
        assert "edit" not in data["tools"]

    def test_test_author_is_told_not_to_touch_production_code(self) -> None:
        _, body = split_frontmatter(AGENTS_DIR / "test-author.agent.md")
        assert "must **not** modify production code" in body

    def test_orchestrator_does_not_hold_edit(self) -> None:
        data, _ = split_frontmatter(AGENTS_DIR / "openapi-to-spring.agent.md")
        assert "edit" not in data["tools"]
        assert "agent" in data["tools"]

    def test_every_agent_can_reach_the_o2j_tools(self) -> None:
        for path in agent_files():
            data, _ = split_frontmatter(path)
            tools = data.get("tools")
            if tools is None:
                continue
            assert any(str(t).startswith("o2j/") for t in tools), (
                f"{path.name} cannot call the o2j MCP tools, so it will improvise shell commands"
            )


class TestGateIntegrity:
    """No agent may be allowed to pass a gate by moving the bar."""

    def test_gate_editing_agents_are_forbidden_from_lowering_thresholds(self) -> None:
        for name in ("test-author", "quality-reviewer", "api-generator"):
            _, body = split_frontmatter(AGENTS_DIR / f"{name}.agent.md")
            lowered = body.lower()
            assert "never" in lowered
            assert any(
                phrase in lowered
                for phrase in ("threshold", "exclusion", "suppress")
            ), f"{name} does not address gate integrity"

    def test_repository_instructions_forbid_lowering_gates(self) -> None:
        body = (REPO_ROOT / ".github" / "copilot-instructions.md").read_text(encoding="utf-8")
        assert "Do not lower a quality gate" in body

    def test_repository_instructions_mark_the_spec_untrusted(self) -> None:
        body = (REPO_ROOT / ".github" / "copilot-instructions.md").read_text(encoding="utf-8")
        assert "untrusted data" in body


class TestInstructionFiles:
    def test_each_instruction_file_declares_apply_to(self) -> None:
        files = sorted(INSTRUCTIONS_DIR.glob("*.instructions.md"))
        assert files, "no path-scoped instruction files found"
        for path in files:
            data, body = split_frontmatter(path)
            assert data.get("applyTo"), f"{path.name} has no applyTo glob, so it never applies"
            assert len(body.strip()) > 200, f"{path.name} is too thin to be useful"


class TestPromptFiles:
    def test_each_prompt_file_targets_an_existing_agent(self) -> None:
        files = sorted(PROMPTS_DIR.glob("*.prompt.md"))
        assert files, "no prompt files found"
        for path in files:
            data, body = split_frontmatter(path)
            agent = data.get("agent")
            assert agent in EXPECTED_AGENTS, f"{path.name} targets unknown agent {agent!r}"
            assert data.get("description"), f"{path.name} has no description"
            assert len(body.strip()) > 100
