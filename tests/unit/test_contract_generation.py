"""Generated contract tests, response conformance and the TIBCO EMS mapping."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest

from o2j import conformance, contracttests, ems
from o2j.config import EmsConfig, EmsRouteConfig, O2JConfig
from o2j.errors import ConfigError
from o2j.models import GenerationPlan
from o2j.samples import SampleError, Sampler, from_pattern
from o2j.specdoc import load_spec
from o2j.stages.generate import generate
from o2j.stages.normalize import normalize
from o2j.stages.plan import plan
from o2j.workdir import WorkDir
from tests.conftest import spec

PKG = "com/acme/commerce/order"


def _setup(cfg: O2JConfig, work: WorkDir, fixture: str) -> tuple[GenerationPlan, dict[str, Any]]:
    result = normalize(cfg, spec(fixture), work=work)
    generation_plan = plan(cfg, work=work)
    work.write_json("plan", "generation-plan.json", generation_plan)
    doc = json.loads(json.dumps(load_spec(Path(result.canonical_path))))
    return generation_plan, doc


# ---- samples ----------------------------------------------------------------------------


class TestSampler:
    def test_samples_validate_against_their_schema(self, cfg: O2JConfig, work: WorkDir) -> None:
        _, doc = _setup(cfg, work, "02-petstore-3.1")
        sampler = Sampler(doc)
        for name in doc["components"]["schemas"]:
            schema = {"$ref": f"#/components/schemas/{name}"}
            assert sampler.is_valid(sampler.sample(schema, for_request=True), schema), name

    def test_exclusive_minimum_is_respected(self) -> None:
        value = Sampler({}).sample({"type": "number", "exclusiveMinimum": 0}, for_request=True)
        assert value > 0

    def test_pattern_sample_matches(self) -> None:
        assert from_pattern("^SKU-[0-9]{4}$") == "SKU-0000"

    def test_unsupported_pattern_is_refused_not_guessed(self) -> None:
        with pytest.raises(SampleError):
            from_pattern("^(a|b)$")

    def test_unsatisfiable_bounds_raise(self) -> None:
        with pytest.raises(SampleError):
            Sampler({}).sample({"type": "integer", "minimum": 5, "maximum": 2}, for_request=True)

    def test_read_only_fields_stay_out_of_requests(self) -> None:
        schema = {
            "type": "object",
            "properties": {"id": {"type": "string", "readOnly": True}, "name": {"type": "string"}},
        }
        assert Sampler({}).sample(schema, for_request=True) == {"name": "o2j-sample"}


# ---- contract case derivation ------------------------------------------------------------


def _markers(cases: contracttests.OperationCases) -> set[str]:
    return {m for c in cases.cases for m in c.markers}


class TestDerivation:
    def test_petstore_31_cases(self, cfg: O2JConfig, work: WorkDir) -> None:
        generation_plan, doc = _setup(cfg, work, "02-petstore-3.1")
        derived = contracttests.derive(generation_plan, doc)
        assert _markers(derived["createOrder"]) == {
            "o2j:createOrder:slice:happy-path-status",
            "o2j:createOrder:contract:response-schema",
            "o2j:createOrder:slice:400-problem-details",
            "o2j:createOrder:slice:malformed-json-400",
            "o2j:createOrder:slice:unknown-enum-value-400",
            "o2j:createOrder:slice:missing-required-param-400",
            "o2j:createOrder:slice:idempotent-replay",
        }
        assert "o2j:getOrder:slice:404-problem-details" in _markers(derived["getOrder"])
        assert "o2j:getOrder:slice:wrong-param-type-400" in _markers(derived["getOrder"])
        assert "o2j:listOrders:slice:invalid-page-size-400" in _markers(derived["listOrders"])

    def test_business_cases_are_never_generated(self, cfg: O2JConfig, work: WorkDir) -> None:
        generation_plan, doc = _setup(cfg, work, "02-petstore-3.1")
        for cases in contracttests.derive(generation_plan, doc).values():
            for marker in _markers(cases):
                assert ":unit:" not in marker, marker
                assert "pagination-empty" not in marker and "201-location" not in marker

    def test_every_generated_marker_is_a_required_case(
        self, cfg: O2JConfig, work: WorkDir
    ) -> None:
        generation_plan, doc = _setup(cfg, work, "02-petstore-3.1")
        required = {op.marker(c) for op in generation_plan.operations for c in op.required_tests}
        for cases in contracttests.derive(generation_plan, doc).values():
            assert _markers(cases) <= required

    def test_path_parameters_do_not_require_a_missing_param_case(
        self, cfg: O2JConfig, work: WorkDir
    ) -> None:
        generation_plan, _ = _setup(cfg, work, "02-petstore-3.1")
        get_order = next(o for o in generation_plan.operations if o.operation_id == "getOrder")
        assert "slice:missing-required-param-400" not in get_order.required_tests

    def test_java_literal_escaping(self) -> None:
        assert contracttests._j('a"b\\c') == '"a\\"b\\\\c"'
        assert "\\u007b" in contracttests._j("{{name}}")
        assert contracttests._j('{"a":{}}') == '"{\\"a\\":{}}"'


# ---- conformance ------------------------------------------------------------------------


PET = {"id": "p1", "name": "Rex", "kind": "DOG"}


class TestConformance:
    PET: ClassVar[dict[str, str]] = PET

    def _check(
        self, cfg: O2JConfig, work: WorkDir, tmp_path: Path, exchanges: list[dict[str, Any]]
    ) -> conformance.ConformanceResult:
        generation_plan, doc = _setup(cfg, work, "01-petstore-3.0")
        out = tmp_path / "svc/target/o2j-conformance"
        out.mkdir(parents=True)
        (out / "exchanges.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in exchanges), encoding="utf-8"
        )
        return conformance.check(tmp_path / "svc", doc, generation_plan.base_path)

    def _exchange(self, status: int, body: Any, ct: str = "application/json") -> dict[str, Any]:
        return {"method": "POST", "path": "/v1/pets", "status": status, "contentType": ct,
                "body": json.dumps(body) if body is not None else ""}

    def test_conforming_response_passes(
        self, cfg: O2JConfig, work: WorkDir, tmp_path: Path
    ) -> None:
        result = self._check(cfg, work, tmp_path, [self._exchange(201, self.PET)])
        assert result.violations == []
        assert result.by_operation["createPet"].conforming_success == 1

    @pytest.mark.parametrize(
        ("exchange", "expected"),
        [
            ({"status": 418}, "status 418 is not declared"),
            ({"status": 501}, "still a stub"),
            ({"ct": "text/plain"}, "content type text/plain is not declared"),
            ({"body": {"id": "p1", "name": "Rex"}}, "'kind' is a required property"),
            ({"body": {**PET, "kind": "BIRD"}}, "is not one of"),
        ],
    )
    def test_violations_are_reported(
        self,
        cfg: O2JConfig,
        work: WorkDir,
        tmp_path: Path,
        exchange: dict[str, Any],
        expected: str,
    ) -> None:
        record = self._exchange(
            exchange.get("status", 201),
            exchange.get("body", self.PET),
            exchange.get("ct", "application/json"),
        )
        result = self._check(cfg, work, tmp_path, [record])
        assert any(expected in v for v in result.violations), result.violations

    def test_exchanges_outside_the_spec_are_ignored(
        self, cfg: O2JConfig, work: WorkDir, tmp_path: Path
    ) -> None:
        stray = {"method": "GET", "path": "/actuator/health", "status": 200,
                 "contentType": "application/json", "body": "{}"}
        result = self._check(cfg, work, tmp_path, [stray])
        assert result.ignored == 1 and result.checked == 0


# ---- TIBCO EMS mapping ------------------------------------------------------------------


def _with_ems(cfg: O2JConfig, *routes: EmsRouteConfig) -> O2JConfig:
    return cfg.model_copy(update={"ems": EmsConfig(enabled=True, routes=list(routes))})


CREATE = EmsRouteConfig(
    destination="ORDERS.CREATE.REQUEST",
    operation="createOrder",
    params={"Idempotency-Key": "jms:JMSMessageID"},
    faults={409: "ORD-DUPLICATE"},
)


class TestEmsMapping:
    def test_valid_mapping_plans_routes_and_required_cases(
        self, cfg: O2JConfig, work: WorkDir
    ) -> None:
        normalize(cfg, spec("02-petstore-3.1"), work=work)
        result = plan(_with_ems(cfg, CREATE), work=work)
        [route] = result.ems_routes
        assert route.path == "/v1/orders"
        assert route.reply_timeout_ms == 5000
        assert {p.name: p.source for p in route.params} == {"Idempotency-Key": "jms:JMSMessageID"}
        create = next(o for o in result.operations if o.operation_id == "createOrder")
        assert {"ems:reply-correlated", "ems:timeout-504", "ems:fault-mapped:409"} <= set(
            create.required_tests
        )

    @pytest.mark.parametrize(
        ("route", "message"),
        [
            (CREATE.model_copy(update={"operation": "nope"}), "is not in the spec"),
            (CREATE.model_copy(update={"params": {"X-Unknown": "literal:1"}}), "no parameter"),
            (CREATE.model_copy(update={"faults": {404: "ORD-NF"}}), "never returns"),
            (CREATE.model_copy(update={"params": {"Idempotency-Key": "env:HOME"}}), "source"),
            (CREATE.model_copy(update={"reply_timeout_ms": -1}), "must be positive"),
        ],
    )
    def test_mapping_errors_fail_the_plan(
        self, cfg: O2JConfig, work: WorkDir, route: EmsRouteConfig, message: str
    ) -> None:
        normalize(cfg, spec("02-petstore-3.1"), work=work)
        with pytest.raises(ConfigError, match=message):
            plan(_with_ems(cfg, route), work=work)

    def test_duplicate_destination_fails(self, cfg: O2JConfig, work: WorkDir) -> None:
        normalize(cfg, spec("02-petstore-3.1"), work=work)
        with pytest.raises(ConfigError, match="mapped twice"):
            plan(_with_ems(cfg, CREATE, CREATE), work=work)

    def test_default_param_source_is_a_valid_jms_property_name(self) -> None:
        assert ems.property_name("Idempotency-Key") == "Idempotency_Key"
        assert ems.property_name("1st") == "_1st"

    def test_bridge_is_generated_only_when_mapped(
        self, cfg: O2JConfig, work: WorkDir, tmp_path: Path
    ) -> None:
        normalize(cfg, spec("02-petstore-3.1"), work=work)
        work.write_json("plan", "generation-plan.json", plan(cfg, work=work))
        generate(cfg, None, tmp_path / "plain", work, run_generator=False)
        assert not (tmp_path / f"plain/src/main/java/{PKG}/ems").exists()

        mapped = _with_ems(cfg, CREATE)
        work.write_json("plan", "generation-plan.json", plan(mapped, work=work))
        result = generate(mapped, None, tmp_path / "ems", work, run_generator=False)
        assert f"src/main/java/{PKG}/ems/EmsBridge.java" in result.managed
        assert f"src/main/java/{PKG}/ems/EmsBridgeCredentials.java" in result.scaffolded
        test = (tmp_path / f"ems/src/test/java/{PKG}/ems/EmsBridgeContractTest.java").read_text(
            encoding="utf-8"
        )
        assert "// o2j:createOrder:ems:reply-correlated" in test
        assert "// o2j:createOrder:ems:fault-mapped:409" in test
        pom = (tmp_path / "ems/pom.xml").read_text(encoding="utf-8")
        assert "artemis-jakarta-server" in pom


class TestGeneratedTestAccounting:
    def test_generated_plus_pending_is_every_required_case(
        self, cfg: O2JConfig, work: WorkDir, tmp_path: Path
    ) -> None:
        generation_plan, _ = _setup(cfg, work, "02-petstore-3.1")
        result = generate(cfg, None, tmp_path, work, run_generator=False)
        total = sum(len(o.required_tests) for o in generation_plan.operations)
        assert result.generated_cases + result.pending_cases == total
        skeleton = (tmp_path / f"src/test/java/{PKG}/service/OrderServiceTest.java").read_text(
            encoding="utf-8"
        )
        assert "// o2j:createOrder:unit:happy-path" in skeleton
        assert '@Disabled("TODO(O2J): write createOrder unit:happy-path")' in skeleton


class TestIdempotency:
    """FR-I-08: an Idempotency-Key operation gets a generated filter and a replay contract case."""

    def test_filter_is_generated_for_declaring_operations_only(
        self, cfg: O2JConfig, work: WorkDir, tmp_path: Path
    ) -> None:
        _setup(cfg, work, "02-petstore-3.1")
        result = generate(cfg, None, tmp_path, work, run_generator=False)
        filter_java = f"src/main/java/{PKG}/config/IdempotencyFilter.java"
        assert filter_java in result.managed
        assert f"src/test/java/{PKG}/config/IdempotencyFilterTest.java" in result.managed
        source = (tmp_path / filter_java).read_text(encoding="utf-8")
        assert 'operation("POST", "/v1/orders") // createOrder' in source
        assert "getOrder" not in source
        assert "ttl: ${O2J_IDEMPOTENCY_TTL:PT24H}" in (
            tmp_path / "src/main/resources/application.yml"
        ).read_text(encoding="utf-8")

    def test_no_filter_without_an_idempotency_header(
        self, cfg: O2JConfig, work: WorkDir, tmp_path: Path
    ) -> None:
        _setup(cfg, work, "01-petstore-3.0")
        generate(cfg, None, tmp_path, work, run_generator=False)
        assert not (tmp_path / f"src/main/java/{PKG}/config/IdempotencyFilter.java").exists()
        assert "idempotency:" not in (
            tmp_path / "src/main/resources/application.yml"
        ).read_text(encoding="utf-8")

    def test_replay_case_sends_one_key_twice_and_expects_one_delegate_call(
        self, cfg: O2JConfig, work: WorkDir
    ) -> None:
        generation_plan, doc = _setup(cfg, work, "02-petstore-3.1")
        cases = contracttests.derive(generation_plan, doc)["createOrder"].cases
        replay = next(c for c in cases if "o2j:createOrder:slice:idempotent-replay" in c.markers)
        body = "\n".join(replay.lines)
        keys = [line for line in replay.lines if '.header("Idempotency-Key"' in line]
        assert len(keys) == 2 and keys[0] == keys[1]
        assert 'header().string("Idempotent-Replayed", "true")' in body
        assert 'assertThat(stub.calls("createOrder")).isEqualTo(1);' in body

    def test_every_case_uses_its_own_valid_key(self, cfg: O2JConfig, work: WorkDir) -> None:
        """A shared key would let one case's stored response replay into the next."""
        generation_plan, doc = _setup(cfg, work, "02-petstore-3.1")
        cases = contracttests.derive(generation_plan, doc)["createOrder"].cases
        per_case = {
            c.method_name: {line.strip() for line in c.lines if '.header("Idempotency-Key"' in line}
            for c in cases
        }
        keys = [next(iter(v)) for v in per_case.values() if v]
        assert len(keys) == len(set(keys)) >= 5
        for key_line in keys:
            value = key_line.split('", "', 1)[1].rstrip('")')
            assert 8 <= len(value) <= 64  # the header's declared minLength / maxLength

    def test_distinct_key_respects_uuid_format_and_pattern(self) -> None:
        uuid_key = contracttests._distinct_key({"format": "uuid"}, "a", "fallback")
        assert len(uuid_key) == 36 and uuid_key.count("-") == 4
        assert contracttests._distinct_key({"pattern": "^[A-Z]+$"}, "a", "KEEP") == "KEEP"
        assert contracttests._distinct_key({}, "a", "x") != contracttests._distinct_key({}, "b", "x")
