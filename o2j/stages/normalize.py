"""S2 Normalize - convert the spec to the one version the Java toolchain can consume.

This stage exists because of a concrete tooling gap. ``openapi-generator`` parses through
``swagger-parser``, which does not support OpenAPI 3.2.0 (swagger-api/swagger-parser#2248),
so a 3.2 document cannot reach the generator at all. Rather than refuse 3.2 input, the
pipeline downgrades it to 3.1.2 and is explicit about what that costs.

Every step is recorded, and the output is byte-stable: the canonical spec's hash is the
identity of the generated code, so an unchanged input must always serialise identically.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from o2j.config import O2JConfig
from o2j.errors import LoadBearingLoss
from o2j.models import (
    DowngradeEntry,
    DowngradeReport,
    NormalizationResult,
    Redaction,
    SanitizationReport,
)
from o2j.rules.naming import PROBLEM_JSON
from o2j.rules.semantic import INJECTION_PATTERNS, NATURAL_LANGUAGE_FIELDS
from o2j.specdoc import (
    HTTP_METHODS,
    detect_version,
    dump_spec,
    iter_operations,
    iter_schemas,
    load_spec,
    pointer,
    sha256_text,
    version_family,
)
from o2j.workdir import WorkDir

# Canonical top-level and operation key order. Sorting everything alphabetically would also
# be deterministic but would put `openapi` after `info`, which makes the canonical document
# needlessly hard to read in a diff.
KEY_PRIORITY: tuple[str, ...] = (
    "openapi",
    "swagger",
    "info",
    "jsonSchemaDialect",
    "servers",
    "security",
    "tags",
    "externalDocs",
    "paths",
    "webhooks",
    "components",
    "operationId",
    "summary",
    "description",
    "tags",
    "parameters",
    "requestBody",
    "responses",
    "type",
    "format",
    "properties",
    "required",
    "items",
)

# 3.2 constructs and whether they survive a downgrade to 3.1. "load-bearing" means the API's
# behaviour changes if we drop it, which stops the run rather than emitting a warning nobody
# reads.
OAS32_CONSTRUCTS: dict[str, tuple[Literal["degradable", "load-bearing"], str, str]] = {
    "additionalOperations": (
        "load-bearing",
        "dropped",
        "Non-standard HTTP methods disappear entirely; clients calling them would 405.",
    ),
    "querystring": (
        "load-bearing",
        "dropped",
        "Whole-querystring schemas have no 3.1 representation, so query validation is lost.",
    ),
    "$self": (
        "degradable",
        "folded into servers",
        "Document self-identity is advisory and does not affect generated code.",
    ),
}


def normalize(
    cfg: O2JConfig,
    spec_path: str | Path | None = None,
    target_version: str | None = None,
    work: WorkDir | None = None,
) -> NormalizationResult:
    """Produce ``spec.canonical.yaml`` plus the downgrade and sanitization reports."""
    work = work or WorkDir()
    path = (
        Path(spec_path)
        if spec_path is not None
        else work.path("ingest", "spec.raw.yaml", create=False)
    )
    if not path.is_file():
        path = cfg.project_root / cfg.openapi.input

    doc = load_spec(path)
    source_version = detect_version(doc)
    target = target_version or cfg.openapi.canonical_version
    applied: list[str] = []
    downgrade = DowngradeReport(
        source_version=source_version, target_version=target, entries=[]
    )
    sanitization = SanitizationReport()

    # 1. Swagger 2.0 up to OpenAPI 3.0.4.
    if version_family(source_version) == "2.0":
        _convert_swagger2(doc, applied)
        source_family = "3.0"
    else:
        source_family = version_family(source_version)

    # 2. OpenAPI 3.2 down to 3.1.
    if source_family == "3.2":
        _downgrade_32(doc, downgrade, applied)
        losses = downgrade.load_bearing_losses
        if losses:
            detail = "; ".join(f"{e.construct_name} at {e.pointer}" for e in losses)
            raise LoadBearingLoss(
                f"OpenAPI 3.2 to {target} downgrade would lose behaviour: {detail}",
                hint=(
                    "These constructs have no 3.1 equivalent. Express them with standard HTTP "
                    "methods, or wait for swagger-parser 3.2 support "
                    "(swagger-api/swagger-parser#2248)."
                ),
            )

    # 3. Sanitize natural-language fields before any agent reads the document.
    _sanitize(doc, sanitization, applied)

    # 4. Canonicalise nullability and numeric bounds to the target version's single form.
    _canonicalize_schemas(doc, target, applied)

    # 5. Hoist inline object schemas to stable, spec-derived names.
    _hoist_inline_schemas(doc, applied)

    # 6. Set the declared version.
    doc.pop("swagger", None)
    doc["openapi"] = target

    # 7. Stable serialise.
    ordered = _order_keys(doc)
    text = dump_spec(ordered)
    canonical = work.write_text("normalize", "spec.canonical.yaml", text)
    applied.append("stable-serialize")

    if downgrade.entries:
        work.write_json("normalize", "downgrade-report.json", downgrade)
    if sanitization.redactions:
        work.write_json("normalize", "sanitization-report.json", sanitization)

    return NormalizationResult(
        canonical_path=str(canonical),
        canonical_hash=sha256_text(text),
        source_version=source_version,
        target_version=target,
        normalizations=applied,
        downgrade_report=downgrade if downgrade.entries else None,
        sanitization_report=sanitization if sanitization.redactions else None,
    )


def _convert_swagger2(doc: CommentedMap, applied: list[str]) -> None:
    """FR-N-01 - Swagger 2.0 to OpenAPI 3.0.4.

    Covers the constructs that change shape rather than merely move: definitions, body and
    formData parameters, produces/consumes, and the host/basePath/schemes triple.
    """
    components: CommentedMap = doc.setdefault("components", CommentedMap())

    if "definitions" in doc:
        components["schemas"] = doc.pop("definitions")
        _rewrite_refs(doc, "#/definitions/", "#/components/schemas/")
    if "securityDefinitions" in doc:
        components["securitySchemes"] = doc.pop("securityDefinitions")
        _rewrite_refs(doc, "#/securityDefinitions/", "#/components/securitySchemes/")
    if "responses" in doc and isinstance(doc.get("responses"), CommentedMap):
        components["responses"] = doc.pop("responses")
        _rewrite_refs(doc, "#/responses/", "#/components/responses/")

    schemes = doc.pop("schemes", None) or ["https"]
    host = doc.pop("host", None)
    base_path = doc.pop("basePath", "") or ""
    if host:
        servers = CommentedSeq()
        for scheme in schemes:
            servers.append(CommentedMap({"url": f"{scheme}://{host}{base_path}"}))
        doc["servers"] = servers
    elif base_path:
        doc["servers"] = CommentedSeq([CommentedMap({"url": base_path})])

    global_produces = doc.pop("produces", None) or ["application/json"]
    global_consumes = doc.pop("consumes", None) or ["application/json"]

    for _path, _method, op in iter_operations(doc):
        produces = op.pop("produces", None) or global_produces
        consumes = op.pop("consumes", None) or global_consumes

        params = op.get("parameters")
        if isinstance(params, CommentedSeq):
            remaining = CommentedSeq()
            body_schema: Any = None
            form_props = CommentedMap()
            required_form: list[str] = []

            for param in params:
                if not isinstance(param, CommentedMap):
                    remaining.append(param)
                    continue
                location = param.get("in")
                if location == "body":
                    body_schema = param.get("schema")
                elif location == "formData":
                    name = str(param.get("name"))
                    prop = CommentedMap(
                        {k: v for k, v in param.items() if k not in ("in", "name", "required")}
                    )
                    form_props[name] = prop
                    if param.get("required"):
                        required_form.append(name)
                else:
                    # In 2.0 the type sits on the parameter; in 3.x it moves into a schema.
                    if "type" in param and "schema" not in param:
                        schema = CommentedMap()
                        for key in ("type", "format", "enum", "items", "minimum", "maximum"):
                            if key in param:
                                schema[key] = param.pop(key)
                        param["schema"] = schema
                    remaining.append(param)

            if remaining:
                op["parameters"] = remaining
            else:
                op.pop("parameters", None)

            if body_schema is not None:
                op["requestBody"] = CommentedMap(
                    {
                        "required": True,
                        "content": CommentedMap(
                            {mt: CommentedMap({"schema": body_schema}) for mt in consumes}
                        ),
                    }
                )
            elif form_props:
                form_schema = CommentedMap({"type": "object", "properties": form_props})
                if required_form:
                    form_schema["required"] = CommentedSeq(required_form)
                op["requestBody"] = CommentedMap(
                    {
                        "content": CommentedMap(
                            {
                                "application/x-www-form-urlencoded": CommentedMap(
                                    {"schema": form_schema}
                                )
                            }
                        )
                    }
                )

        responses = op.get("responses")
        if isinstance(responses, CommentedMap):
            for status, response in responses.items():
                if not isinstance(response, CommentedMap):
                    continue
                response_schema: Any = response.pop("schema", None)
                if response_schema is None:
                    continue
                # Swagger 2.0 cannot express a per-response media type: `produces` applies to
                # the whole operation. Conversion is therefore the only place the house error
                # contract can be applied, so error responses are mapped to problem+json
                # rather than inheriting `produces` and failing the standards gate with a
                # finding the author cannot fix in a 2.0 document.
                media_types = (
                    [PROBLEM_JSON] if str(status)[:1] in ("4", "5") else list(produces)
                )
                response["content"] = CommentedMap(
                    {mt: CommentedMap({"schema": response_schema}) for mt in media_types}
                )

    applied.append("convert-swagger-2.0-to-3.0.4")
    applied.append("apply-problem-json-to-error-responses")


def _rewrite_refs(node: Any, old: str, new: str) -> None:
    if isinstance(node, CommentedMap):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith(old):
            node["$ref"] = new + ref[len(old) :]
        for value in node.values():
            _rewrite_refs(value, old, new)
    elif isinstance(node, CommentedSeq):
        for value in node:
            _rewrite_refs(value, old, new)


def _downgrade_32(doc: CommentedMap, report: DowngradeReport, applied: list[str]) -> None:
    """FR-N-02 - OpenAPI 3.2 to 3.1.2, classifying every loss."""
    paths = doc.get("paths")
    if isinstance(paths, CommentedMap):
        for path, item in paths.items():
            if not isinstance(item, CommentedMap):
                continue
            for construct in ("additionalOperations", "querystring"):
                if construct in item:
                    classification, action, impact = OAS32_CONSTRUCTS[construct]
                    report.entries.append(
                        DowngradeEntry(
                            construct_name=construct,
                            pointer=pointer("paths", str(path), construct),
                            classification=classification,
                            action=action,
                            impact=impact,
                        )
                    )

    if "$self" in doc:
        classification, action, impact = OAS32_CONSTRUCTS["$self"]
        doc.pop("$self")
        report.entries.append(
            DowngradeEntry(
                construct_name="$self",
                pointer="#/$self",
                classification=classification,
                action=action,
                impact=impact,
            )
        )

    # Nested tag hierarchies flatten: `parent` is dropped and the tag survives standalone.
    tags = doc.get("tags")
    if isinstance(tags, CommentedSeq):
        for i, tag in enumerate(tags):
            if isinstance(tag, CommentedMap) and ("parent" in tag or "kind" in tag):
                tag.pop("parent", None)
                tag.pop("kind", None)
                report.entries.append(
                    DowngradeEntry(
                        construct_name="tag-hierarchy",
                        pointer=pointer("tags", i),
                        classification="degradable",
                        action="flattened to a standalone tag",
                        impact=(
                            "Interface and controller grouping is unaffected; only "
                            "documentation nesting is lost."
                        ),
                    )
                )

    applied.append("downgrade-3.2-to-3.1")


def _sanitize(doc: CommentedMap, report: SanitizationReport, applied: list[str]) -> None:
    """FR-V-13 / FR-N-03 - redact agent-directed text from natural-language fields.

    The spec is attacker-influenced input that Copilot agents read. Redaction happens before
    any agent sees the canonical document, and every removal is recorded so a human can tell
    what was taken out and from where.
    """
    count = 0

    def walk(node: Any, trail: list[str | int]) -> None:
        nonlocal count
        if isinstance(node, CommentedMap):
            for key in list(node.keys()):
                value = node[key]
                name = str(key)
                if name in NATURAL_LANGUAGE_FIELDS and isinstance(value, str):
                    cleaned = value
                    for pattern_name, pattern in INJECTION_PATTERNS:
                        match = pattern.search(cleaned)
                        if not match:
                            continue
                        report.redactions.append(
                            Redaction(
                                pointer=pointer(*trail, name),
                                pattern=pattern_name,
                                excerpt=match.group(0)[:120],
                            )
                        )
                        cleaned = pattern.sub("[redacted by o2j]", cleaned)
                        count += 1
                    if cleaned != value:
                        node[key] = cleaned
                walk(value, [*trail, name])
        elif isinstance(node, CommentedSeq):
            for i, value in enumerate(node):
                walk(value, [*trail, i])

    walk(doc, [])
    if count:
        applied.append(f"sanitize-descriptions:{count}")


def _canonicalize_schemas(doc: CommentedMap, target: str, applied: list[str]) -> None:
    """FR-N-03 / FR-N-04 - one representation for nullability and exclusive bounds."""
    family = version_family(target)
    nullable_fixes = 0
    bound_fixes = 0

    for _ptr, schema in iter_schemas(doc):
        if family in ("3.1", "3.2") and schema.pop("nullable", None) is True:
            current = schema.get("type")
            if isinstance(current, str):
                schema["type"] = CommentedSeq([current, "null"])
                nullable_fixes += 1
            elif isinstance(current, CommentedSeq) and "null" not in list(current):
                current.append("null")
                nullable_fixes += 1

        if family in ("3.1", "3.2"):
            for key, paired in (
                ("exclusiveMinimum", "minimum"),
                ("exclusiveMaximum", "maximum"),
            ):
                if isinstance(schema.get(key), bool):
                    if schema[key] is True and paired in schema:
                        schema[key] = schema.pop(paired)
                    else:
                        schema.pop(key)
                    bound_fixes += 1

    if nullable_fixes:
        applied.append(f"canonicalize-nullability:{nullable_fixes}")
    if bound_fixes:
        applied.append(f"canonicalize-exclusive-bounds:{bound_fixes}")


def _hoist_inline_schemas(doc: CommentedMap, applied: list[str]) -> None:
    """FR-N-06 - move inline object schemas into components under stable names.

    Names are derived from the operation and context, never from an ordinal. An ordinal name
    like ``InlineObject3`` renumbers when an unrelated operation is inserted, which silently
    renames a generated Java class and every import of it.
    """
    components: CommentedMap = doc.setdefault("components", CommentedMap())
    schemas: CommentedMap = components.setdefault("schemas", CommentedMap())
    hoisted = 0

    for path, method, op in iter_operations(doc):
        op_id = str(op.get("operationId") or f"{method}{_pascal(path)}")

        body = op.get("requestBody")
        if isinstance(body, CommentedMap):
            hoisted += _hoist_from_content(body, schemas, f"{_pascal(op_id)}Request")

        responses = op.get("responses")
        if isinstance(responses, CommentedMap):
            for status, response in responses.items():
                if not isinstance(response, CommentedMap):
                    continue
                suffix = "Response" if str(status).startswith("2") else f"Error{status}"
                hoisted += _hoist_from_content(
                    response, schemas, f"{_pascal(op_id)}{suffix}"
                )

    if not schemas:
        components.pop("schemas", None)
    if not components:
        doc.pop("components", None)
    if hoisted:
        applied.append(f"hoist-inline-schemas:{hoisted}")


def _hoist_from_content(holder: CommentedMap, schemas: CommentedMap, base: str) -> int:
    content = holder.get("content")
    if not isinstance(content, CommentedMap):
        return 0
    moved = 0
    for media in content.values():
        if not isinstance(media, CommentedMap):
            continue
        schema = media.get("schema")
        if not isinstance(schema, CommentedMap) or "$ref" in schema:
            continue
        if schema.get("type") != "object" and "properties" not in schema:
            continue
        name = _unique_name(base, schemas)
        schemas[name] = schema
        media["schema"] = CommentedMap({"$ref": f"#/components/schemas/{name}"})
        moved += 1
    return moved


def _unique_name(base: str, schemas: CommentedMap) -> str:
    if base not in schemas:
        return base
    # A collision means two operations derived the same name; disambiguate by appending a
    # letter rather than a number, so the suffix cannot be mistaken for an ordinal.
    for suffix in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        candidate = f"{base}{suffix}"
        if candidate not in schemas:
            return candidate
    return f"{base}X"


def _pascal(value: str) -> str:
    parts = [p for p in re.split(r"[^a-zA-Z0-9]+", value) if p]
    return "".join(p[:1].upper() + p[1:] for p in parts)


def _order_keys(node: Any) -> Any:
    """FR-N-07 - deterministic key order throughout the document."""
    if isinstance(node, CommentedMap):
        out = CommentedMap()
        keys = list(node.keys())
        known = [k for k in KEY_PRIORITY if k in keys]
        rest = sorted(k for k in keys if k not in KEY_PRIORITY)
        seen: set[str] = set()
        for key in [*known, *rest]:
            if key in seen:
                continue
            seen.add(key)
            out[key] = _order_keys(node[key])
        return out
    if isinstance(node, CommentedSeq):
        return CommentedSeq([_order_keys(v) for v in node])
    return node


__all__ = ["HTTP_METHODS", "normalize"]
