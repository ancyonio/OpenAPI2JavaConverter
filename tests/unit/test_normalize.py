"""Normalization: version conversion, canonicalisation, and byte stability."""

from __future__ import annotations

from pathlib import Path

import pytest

from o2j.config import O2JConfig
from o2j.errors import ExitCode, LoadBearingLoss
from o2j.specdoc import load_spec
from o2j.stages.normalize import normalize
from o2j.workdir import WorkDir
from tests.conftest import spec


class TestDeterminism:
    """The canonical spec's hash is the identity of the generated code, so it must be stable."""

    @pytest.mark.parametrize(
        "fixture", ["00-minimal", "01-petstore-3.0", "02-petstore-3.1", "03-swagger-2.0"]
    )
    def test_two_runs_produce_identical_output(
        self, cfg: O2JConfig, tmp_path: Path, fixture: str
    ) -> None:
        hashes = []
        for i in range(2):
            result = normalize(cfg, spec(fixture), work=WorkDir(tmp_path / f"run{i}"))
            hashes.append(result.canonical_hash)
        assert hashes[0] == hashes[1]

    def test_output_has_no_crlf(self, cfg: O2JConfig, work: WorkDir) -> None:
        """A CRLF artifact on Windows would hash differently from the same run on Linux CI."""
        result = normalize(cfg, spec("02-petstore-3.1"), work=work)
        raw = Path(result.canonical_path).read_bytes()
        assert b"\r\n" not in raw


class TestOpenApi32Downgrade:
    """3.2 cannot reach openapi-generator, so the downgrade must be explicit about its cost."""

    def test_load_bearing_loss_stops_the_run(self, cfg: O2JConfig, work: WorkDir) -> None:
        with pytest.raises(LoadBearingLoss) as caught:
            normalize(cfg, spec("04-oas-3.2"), work=work)

        assert caught.value.exit_code == ExitCode.LOAD_BEARING_LOSS
        assert "additionalOperations" in caught.value.message
        assert caught.value.hint is not None
        # The hint must point at the real cause rather than suggesting a workaround.
        assert "swagger-parser" in caught.value.hint

    def test_degradable_losses_alone_do_not_stop_the_run(
        self, cfg: O2JConfig, work: WorkDir, tmp_path: Path
    ) -> None:
        """With the non-standard method removed, the nested tag and $self should degrade cleanly."""
        source = (spec("04-oas-3.2")).read_text(encoding="utf-8")
        trimmed = source.split("    additionalOperations:")[0]
        trimmed += "\ncomponents:" + source.split("\ncomponents:", 1)[1]
        target = tmp_path / "trimmed.yaml"
        target.write_text(trimmed, encoding="utf-8")

        result = normalize(cfg, target, work=work)

        assert result.target_version == "3.1.2"
        assert result.downgrade_report is not None
        classifications = {e.classification for e in result.downgrade_report.entries}
        assert classifications == {"degradable"}
        assert not result.downgrade_report.load_bearing_losses


class TestSwagger2Conversion:
    def test_definitions_move_to_components(self, cfg: O2JConfig, work: WorkDir) -> None:
        result = normalize(cfg, spec("03-swagger-2.0"), work=work)
        doc = load_spec(Path(result.canonical_path))

        assert "definitions" not in doc
        assert "Customer" in doc["components"]["schemas"]
        assert doc["openapi"] == "3.1.2"

    def test_body_parameter_becomes_request_body(self, cfg: O2JConfig, work: WorkDir) -> None:
        result = normalize(cfg, spec("03-swagger-2.0"), work=work)
        doc = load_spec(Path(result.canonical_path))

        post = doc["paths"]["/customers"]["post"]
        assert "requestBody" in post
        assert "application/json" in post["requestBody"]["content"]
        assert not any(p.get("in") == "body" for p in post.get("parameters", []))

    def test_host_and_base_path_become_servers(self, cfg: O2JConfig, work: WorkDir) -> None:
        result = normalize(cfg, spec("03-swagger-2.0"), work=work)
        doc = load_spec(Path(result.canonical_path))
        assert doc["servers"][0]["url"] == "https://legacy.example.com/api/v1"

    def test_response_schema_becomes_content(self, cfg: O2JConfig, work: WorkDir) -> None:
        result = normalize(cfg, spec("03-swagger-2.0"), work=work)
        doc = load_spec(Path(result.canonical_path))
        ok = doc["paths"]["/customers"]["get"]["responses"]["200"]
        assert "schema" not in ok
        assert "application/json" in ok["content"]


class TestCanonicalisation:
    def test_nullable_becomes_a_type_union_for_a_31_target(
        self, cfg: O2JConfig, work: WorkDir
    ) -> None:
        result = normalize(cfg, spec("01-petstore-3.0"), work=work)
        doc = load_spec(Path(result.canonical_path))

        adopted = doc["components"]["schemas"]["Pet"]["properties"]["adoptedAt"]
        assert "nullable" not in adopted
        assert set(adopted["type"]) == {"string", "null"}

    def test_boolean_exclusive_minimum_becomes_numeric(
        self, cfg: O2JConfig, work: WorkDir
    ) -> None:
        result = normalize(cfg, spec("01-petstore-3.0"), work=work)
        doc = load_spec(Path(result.canonical_path))

        weight = doc["components"]["schemas"]["CreatePetRequest"]["properties"]["weightKg"]
        assert weight["exclusiveMinimum"] == 0
        assert "minimum" not in weight

    def test_inline_schemas_get_spec_derived_names(self, cfg: O2JConfig, work: WorkDir) -> None:
        """Names must come from the operation, never from an ordinal.

        An ordinal renumbers when the document is edited, silently renaming a generated class.
        """
        result = normalize(cfg, spec("05-pathological"), work=work)
        doc = load_spec(Path(result.canonical_path))

        names = list(doc["components"]["schemas"].keys())
        hoisted = [n for n in names if "Request" in n or "Response" in n]
        assert hoisted, "no inline schema was hoisted"
        assert not any(n.startswith("InlineObject") for n in names)
        assert not any(n[-1].isdigit() for n in hoisted), hoisted


class TestSanitization:
    def test_injection_text_is_redacted_and_recorded(
        self, cfg: O2JConfig, work: WorkDir
    ) -> None:
        result = normalize(cfg, spec("05-pathological"), work=work)

        assert result.sanitization_report is not None
        assert result.sanitization_report.redactions

        canonical = Path(result.canonical_path).read_text(encoding="utf-8")
        assert "Ignore previous instructions" not in canonical
        assert "[redacted by o2j]" in canonical
