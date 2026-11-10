"""Planning, and the derived required-test matrix.

The required-test derivation is what turns the coverage requirement from an aspiration into a
checkable contract, so it gets the most direct tests in the suite.
"""

from __future__ import annotations

import pytest

from o2j.config import O2JConfig
from o2j.stages.normalize import normalize
from o2j.stages.plan import plan
from o2j.workdir import WorkDir
from tests.conftest import spec


@pytest.fixture
def order_plan(cfg: O2JConfig, work: WorkDir):
    normalize(cfg, spec("02-petstore-3.1"), work=work)
    return plan(cfg, work=work)


class TestInventory:
    def test_all_operations_are_found(self, order_plan) -> None:
        ids = {op.operation_id for op in order_plan.operations}
        assert ids == {"listOrders", "createOrder", "getOrder", "cancelOrder"}

    def test_operations_group_by_tag(self, order_plan) -> None:
        assert [r.tag for r in order_plan.resources] == ["orders"]
        resource = order_plan.resources[0]
        assert resource.api_interface == "OrdersApi"
        assert resource.delegate == "OrdersApiDelegateImpl"
        assert resource.service == "OrderService"  # singularised

    def test_target_is_recorded_from_config(self, order_plan, cfg: O2JConfig) -> None:
        assert order_plan.target.java_version == cfg.target.java_version
        assert order_plan.target.java_tier == 1
        assert order_plan.target.spring_boot_version == cfg.versions.spring_boot


class TestOperationShape:
    def test_create_is_detected_as_idempotent(self, order_plan) -> None:
        create = next(o for o in order_plan.operations if o.operation_id == "createOrder")
        assert create.idempotent is True
        assert create.success_status == 201
        assert create.request_type == "CreateOrderRequest"
        assert create.response_type == "Order"
        assert 409 in create.error_statuses

    def test_list_is_detected_as_paginated(self, order_plan) -> None:
        listing = next(o for o in order_plan.operations if o.operation_id == "listOrders")
        assert listing.paginated is True

    def test_cancel_is_detected_as_optimistically_concurrent(self, order_plan) -> None:
        cancel = next(o for o in order_plan.operations if o.operation_id == "cancelOrder")
        assert cancel.optimistic_concurrency is True
        assert cancel.success_status == 204

    def test_security_scopes_are_resolved(self, order_plan) -> None:
        create = next(o for o in order_plan.operations if o.operation_id == "createOrder")
        assert create.security == ["oauth2:orders:write"]

    def test_enum_fields_are_found_through_refs(self, order_plan) -> None:
        create = next(o for o in order_plan.operations if o.operation_id == "createOrder")
        assert "status" in create.enum_fields


class TestRequiredTests:
    """Each required case must follow from the operation's shape, not from a fixed list."""

    def test_every_operation_has_the_universal_cases(self, order_plan) -> None:
        for op in order_plan.operations:
            assert "unit:happy-path" in op.required_tests
            assert "contract:response-schema" in op.required_tests

    def test_create_requires_location_header_and_conflict_cases(self, order_plan) -> None:
        create = next(o for o in order_plan.operations if o.operation_id == "createOrder")
        assert "slice:201-location-header" in create.required_tests
        assert "unit:conflict-409" in create.required_tests
        assert "slice:idempotent-replay" in create.required_tests

    def test_request_body_requires_validation_cases(self, order_plan) -> None:
        create = next(o for o in order_plan.operations if o.operation_id == "createOrder")
        assert "slice:400-problem-details" in create.required_tests
        assert "slice:malformed-json-400" in create.required_tests

    def test_paginated_operation_requires_boundary_cases(self, order_plan) -> None:
        listing = next(o for o in order_plan.operations if o.operation_id == "listOrders")
        for case in (
            "slice:pagination-first-page",
            "slice:pagination-last-page",
            "slice:pagination-empty",
            "slice:pagination-beyond-end",
            "slice:invalid-page-size-400",
        ):
            assert case in listing.required_tests

    def test_secured_operation_requires_auth_cases(self, order_plan) -> None:
        get = next(o for o in order_plan.operations if o.operation_id == "getOrder")
        assert "slice:unauthenticated-401" in get.required_tests
        assert "slice:wrong-scope-403" in get.required_tests

    def test_public_operation_requires_no_auth_cases(self, cfg: O2JConfig, work: WorkDir) -> None:
        normalize(cfg, spec("00-minimal"), work=work)
        public = plan(cfg, work=work).operations[0]
        assert "slice:unauthenticated-401" not in public.required_tests

    def test_concurrency_header_requires_a_412_case(self, order_plan) -> None:
        cancel = next(o for o in order_plan.operations if o.operation_id == "cancelOrder")
        assert "slice:stale-if-match-412" in cancel.required_tests

    def test_cases_are_unique_and_stably_ordered(self, cfg: O2JConfig, work: WorkDir) -> None:
        normalize(cfg, spec("02-petstore-3.1"), work=work)
        first = {o.operation_id: o.required_tests for o in plan(cfg, work=work).operations}
        second = {o.operation_id: o.required_tests for o in plan(cfg, work=work).operations}

        assert first == second
        for cases in first.values():
            assert len(cases) == len(set(cases))


class TestRiskScore:
    def test_clean_spec_is_low_risk(self, order_plan) -> None:
        assert order_plan.risk_score.band == "low"

    def test_polymorphic_schemas_raise_the_band(self, cfg: O2JConfig, work: WorkDir) -> None:
        """A polymorphic request body needs human review of the generated model."""
        normalize(cfg, spec("05-pathological"), work=work)
        risky = plan(cfg, work=work)

        assert risky.risk_score.value > 0
        factors = {f for op in risky.operations for f in op.risk_factors}
        assert "polymorphic-schema" in factors
        assert "discriminator" in factors
