"""The scaffold template renderer.

Small surface, but a renderer bug corrupts generated Java silently - the output still looks
plausible. These tests pin the cases that actually bit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest

from o2j.template import render

TEMPLATE_DIR = Path(__file__).resolve().parent.parent.parent / "o2j" / "templates"


class TestVariables:
    def test_substitutes_a_variable(self) -> None:
        assert render("hello {{name}}", {"name": "world"}) == "hello world"

    def test_substitutes_a_dotted_path(self) -> None:
        assert render("{{a.b.c}}", {"a": {"b": {"c": 7}}}) == "7"

    def test_missing_variable_renders_empty(self) -> None:
        assert render("[{{nope}}]", {}) == "[]"

    def test_booleans_render_lowercase_or_empty(self) -> None:
        assert render("{{on}}/{{off}}", {"on": True, "off": False}) == "true/"

    def test_triple_brace_is_supported(self) -> None:
        assert render("{{{raw}}}", {"raw": "<b>"}) == "<b>"

    def test_double_brace_followed_by_a_literal_brace(self) -> None:
        """Regression: `{{var}}}` must not be read as the triple form.

        An optional-brace pattern consumed the third `}`, which silently removed the closing
        brace of a Javadoc `{@code ...}` in the delegate scaffold.
        """
        assert render("{@code x {{v}}}", {"v": "y"}) == "{@code x y}"


class TestSections:
    def test_iterates_a_list_of_dicts(self) -> None:
        out = render("{{#xs}}[{{n}}]{{/xs}}", {"xs": [{"n": 1}, {"n": 2}]})
        assert out == "[1][2]"

    def test_outer_context_is_visible_inside_a_section(self) -> None:
        out = render("{{#xs}}{{prefix}}{{n}} {{/xs}}", {"prefix": "v", "xs": [{"n": 1}]})
        assert out == "v1 "

    def test_truthy_scalar_section_renders_once(self) -> None:
        assert render("{{#flag}}yes{{/flag}}", {"flag": True}) == "yes"

    def test_falsy_section_renders_nothing(self) -> None:
        assert render("{{#flag}}yes{{/flag}}", {"flag": False}) == ""
        assert render("{{#xs}}yes{{/xs}}", {"xs": []}) == ""

    def test_inverted_section_renders_when_absent(self) -> None:
        assert render("{{^flag}}no{{/flag}}", {"flag": False}) == "no"
        assert render("{{^flag}}no{{/flag}}", {"flag": True}) == ""

    def test_nested_sections(self) -> None:
        out = render(
            "{{#outer}}({{#inner}}{{v}}{{/inner}}){{/outer}}",
            {"outer": True, "inner": [{"v": "a"}, {"v": "b"}]},
        )
        assert out == "(ab)"


class TestShippedTemplates:
    """Every shipped template must render with no token left behind."""

    CONTEXT: ClassVar[dict[str, Any]] = {
        "group_id": "com.example",
        "artifact_id": "demo",
        "service_name": "demo",
        "base_package": "com.example.demo",
        "pkg_path": "com/example/demo",
        "java_version": 21,
        "spring_boot_version": "3.5.16",
        "use_spring_boot_4": False,
        "use_jackson_3": False,
        "virtual_threads": True,
        "spec_hash": "sha256:abc",
        "operation_count": 1,
        "mutation_target": "com.example.demo.service.*",
        "versions": {
            "openapi_generator": "7.27.0",
            "mapstruct": "1.6.3",
            "springdoc": "2.7.0",
            "archunit": "1.3.0",
            "jacoco": "0.8.13",
            "pitest": "1.17.4",
            "pitest_junit5": "1.2.1",
            "spotless": "2.44.0",
            "checkstyle": "3.6.0",
            "pmd": "3.26.0",
            "spotbugs": "4.8.6.6",
            "testcontainers": "1.20.6",
            "owasp_dependency_check": "11.1.0",
            "cyclonedx": "2.9.1",
        },
        "generation": {
            "delegate_pattern": True,
            "interface_only": False,
            "use_tags": True,
            "use_optional": False,
            "use_bean_validation": True,
            "perform_bean_validation": True,
            "open_api_nullable": False,
            "documentation_provider": "springdoc",
            "annotation_library": "swagger2",
            "serializable_model": True,
            "unhandled_exception": True,
        },
        "coverage": {"line_min": 85, "branch_min": 80, "service_layer_line_min": 90},
        "mutation": {"threshold": 70, "coverage_threshold": 85},
        "coverage_excludes": [{"pattern": "**/api/model/**"}],
        "tag": "orders",
        "api_interface": "OrdersApi",
        "delegate": "OrdersApiDelegateImpl",
        "service": "OrderService",
        "operations": [
            {
                "operation_id": "createOrder",
                "method": "POST",
                "path": "/orders",
                "success_status": 201,
                "request_type": "CreateOrderRequest",
                "response_type": "Order",
                "paginated": False,
                "idempotent": True,
                "required_test_count": 16,
            }
        ],
    }

    @pytest.mark.parametrize(
        "path", sorted(TEMPLATE_DIR.glob("*.mustache")), ids=lambda p: p.name
    )
    def test_renders_without_leftover_tokens(self, path: Path) -> None:
        out = render(path.read_text(encoding="utf-8"), self.CONTEXT)
        assert "{{" not in out, f"{path.name} left an unrendered token"
        assert "}}" not in out, f"{path.name} left an unrendered token"

    @pytest.mark.parametrize(
        "path",
        sorted(p for p in TEMPLATE_DIR.glob("*.java.mustache")),
        ids=lambda p: p.name,
    )
    def test_java_braces_balance(self, path: Path) -> None:
        """A renderer that eats a brace produces Java that will not compile."""
        out = render(path.read_text(encoding="utf-8"), self.CONTEXT)
        assert out.count("{") == out.count("}"), f"{path.name} has unbalanced braces"

    def test_pom_declares_the_configured_boot_version(self) -> None:
        out = render((TEMPLATE_DIR / "pom.mustache").read_text(encoding="utf-8"), self.CONTEXT)
        assert "<version>3.5.16</version>" in out
        assert "<useSpringBoot3>true</useSpringBoot3>" in out
        assert "<useSpringBoot4>" not in out

    def test_pom_switches_to_boot_4_profile(self) -> None:
        context = {**self.CONTEXT, "use_spring_boot_4": True, "spring_boot_version": "4.1.0"}
        out = render((TEMPLATE_DIR / "pom.mustache").read_text(encoding="utf-8"), context)
        assert "<version>4.1.0</version>" in out
        assert "<useSpringBoot4>true</useSpringBoot4>" in out
        assert "<useJackson3>true</useJackson3>" in out
        assert "<useSpringBoot3>" not in out

    def test_pom_never_emits_a_generation_timestamp(self) -> None:
        """FR-G-05: a timestamp in generated output breaks the determinism gate."""
        out = render((TEMPLATE_DIR / "pom.mustache").read_text(encoding="utf-8"), self.CONTEXT)
        assert "<hideGenerationTimestamp>true</hideGenerationTimestamp>" in out

    def test_pom_disables_generated_test_stubs(self) -> None:
        out = render((TEMPLATE_DIR / "pom.mustache").read_text(encoding="utf-8"), self.CONTEXT)
        assert "<generateApiTests>false</generateApiTests>" in out
        assert "<generateModelTests>false</generateModelTests>" in out
