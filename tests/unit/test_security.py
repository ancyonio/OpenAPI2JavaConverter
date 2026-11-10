"""Security rules derived from the spec (o2j/security.py) and the plan fields they read."""

from __future__ import annotations

from typing import Any

import pytest

from o2j import security
from o2j.config import O2JConfig
from o2j.models import GenerationPlan, PlannedOperation, PlannedResource, PlanTarget, RiskScore
from o2j.stages.normalize import normalize
from o2j.stages.plan import derive_required_tests, plan
from o2j.workdir import WorkDir
from tests.conftest import spec

SCHEMES = {"oauth2": "oauth2", "bearer": "http:bearer", "key": "apiKey"}


def _op(
    requirements: list[dict[str, list[str]]],
    path: str = "/pets",
    method: str = "GET",
    operation_id: str = "op",
) -> PlannedOperation:
    return PlannedOperation(
        operation_id=operation_id,
        method=method,
        path=path,
        pointer="/paths",
        success_status=200,
        security_requirements=requirements,
    )


def _plan(*ops: PlannedOperation, base_path: str = "", **kw: Any) -> GenerationPlan:
    return GenerationPlan(
        spec_hash="sha256:x",
        spec_version="3.1.2",
        original_version="3.1.2",
        target=PlanTarget(
            java_version=21,
            spring_profile="spring-boot-3",
            spring_boot_version="3.5.16",
            build_tool="maven",
            web_stack="mvc",
            java_tier=1,
        ),
        base_package="com.example",
        base_path=base_path,
        security_schemes=kw.get("schemes", SCHEMES),
        resources=[
            PlannedResource(
                tag="t", api_interface="TApi", delegate="D", service="S", operations=list(ops)
            )
        ],
        risk_score=RiskScore(value=0, band="low"),
    )


def _expr(requirements: list[dict[str, list[str]]]) -> str:
    return security.rules(_plan(_op(requirements)))[0].expression


class TestExpressions:
    def test_no_requirement_is_public(self) -> None:
        assert _expr([]) == "permitAll()"

    def test_anonymous_alternative_is_public(self) -> None:
        assert _expr([{}, {"oauth2": ["pets:read"]}]) == "permitAll()"

    def test_single_scope(self) -> None:
        assert _expr([{"oauth2": ["pets:write"]}]) == 'hasAuthority("SCOPE_pets:write")'

    def test_alternative_scopes_are_or(self) -> None:
        assert (
            _expr([{"oauth2": ["a"]}, {"oauth2": ["b"]}])
            == 'hasAnyAuthority("SCOPE_a", "SCOPE_b")'
        )

    def test_scopes_in_one_requirement_are_and(self) -> None:
        out = _expr([{"oauth2": ["a", "b"]}])
        assert out.startswith("access(AuthorizationManagers.allOf(")
        assert '"SCOPE_a"' in out and '"SCOPE_b"' in out

    def test_or_of_ands(self) -> None:
        out = _expr([{"oauth2": ["a", "b"]}, {"oauth2": ["c"]}])
        assert out.startswith("access(AuthorizationManagers.anyOf(")

    def test_bearer_without_scopes_needs_any_valid_token(self) -> None:
        assert _expr([{"bearer": []}]) == "authenticated()"

    def test_unsupported_scheme_fails_closed(self) -> None:
        rule = security.rules(_plan(_op([{"key": []}])))[0]
        assert rule.expression == "denyAll()"
        assert "not wired" in rule.comment

    def test_unknown_scheme_fails_closed(self) -> None:
        assert _expr([{"undeclared": ["x"]}]) == "denyAll()"


class TestRuleOrdering:
    def test_base_path_prefixes_every_pattern(self) -> None:
        rules = security.rules(_plan(_op([]), base_path="/v1"))
        assert rules[0].pattern == "/v1/pets"

    def test_literal_path_precedes_templated_path(self) -> None:
        """First match wins in Spring Security, so /pets/mine must not be shadowed."""
        rules = security.rules(
            _plan(
                _op([], path="/pets/{id}", operation_id="get"),
                _op([], path="/pets/mine", operation_id="mine"),
            )
        )
        assert [r.operation_id for r in rules] == ["mine", "get"]


class TestJwtDetection:
    def test_uses_jwt_for_oauth2(self) -> None:
        assert security.uses_jwt(_plan(_op([{"oauth2": ["a"]}])))

    def test_no_jwt_for_public_only(self) -> None:
        assert not security.uses_jwt(_plan(_op([])))


class TestHelpers:
    def test_sample_path_fills_template_variables(self) -> None:
        out = security.sample_path("/v1/pets/{id}/toys/{toyId}")
        assert out == "/v1/pets/o2j-test/toys/o2j-test"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("createPet", "createPet"), ("get-pet.v2", "get_pet_v2"), ("1x", "op_1x")],
    )
    def test_java_identifier(self, raw: str, expected: str) -> None:
        assert security.java_identifier(raw) == expected


class TestRequiredAuthCases:
    def test_public_alternative_requires_neither_auth_case(self) -> None:
        cases = derive_required_tests(_op([{}, {"oauth2": ["a"]}]))
        assert "slice:unauthenticated-401" not in cases
        assert "slice:wrong-scope-403" not in cases

    def test_scopeless_bearer_requires_401_but_not_403(self) -> None:
        cases = derive_required_tests(_op([{"bearer": []}]))
        assert "slice:unauthenticated-401" in cases
        assert "slice:wrong-scope-403" not in cases

    def test_scoped_operation_requires_both(self) -> None:
        cases = derive_required_tests(_op([{"oauth2": ["a"]}]))
        assert {"slice:unauthenticated-401", "slice:wrong-scope-403"} <= set(cases)


class TestPlanExtraction:
    def test_petstore_30_base_path_schemes_and_public_operation(
        self, cfg: O2JConfig, work: WorkDir
    ) -> None:
        normalize(cfg, spec("01-petstore-3.0"), work=work)
        result = plan(cfg, work=work)
        assert result.base_path == "/v1"
        assert result.security_schemes == {"oauth2": "oauth2"}
        by_id = {o.operation_id: o for o in result.operations}
        assert by_id["listPets"].security_requirements == []
        assert by_id["createPet"].security_requirements == [{"oauth2": ["pets:write"]}]

    def test_marker_names_the_operation(self) -> None:
        assert _op([], operation_id="createPet").marker("unit:happy-path") == (
            "o2j:createPet:unit:happy-path"
        )
