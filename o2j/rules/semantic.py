"""Semantic checks: specs that are schema-valid but generate wrong Java.

These are the expensive failures. A spec that breaks the OpenAPI JSON Schema fails loudly at
parse time and costs an hour. A 3.0-style ``nullable: true`` in a 3.1 document parses
cleanly, generates a field with the wrong Jackson nullability, and costs a production
incident. Everything in this module exists because a generic linter will wave it through.

See SPECIFICATION.md Section 4.1.1.
"""

from __future__ import annotations

import re
from typing import Any

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from o2j.config import O2JConfig
from o2j.models import Finding, Location
from o2j.specdoc import (
    iter_operations,
    iter_schemas,
    line_of,
    pointer,
    resolve_pointer,
    version_family,
)

CAMEL_CASE = re.compile(r"^[a-z][a-zA-Z0-9]*$")

# Phrases that indicate someone has written instructions for an AI agent into a spec field.
# Matched case-insensitively against natural-language fields only.
INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "instruction-override",
        re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instruction", re.I),
    ),
    ("role-reassignment", re.compile(r"you\s+are\s+now\s+(a|an|the)\b", re.I)),
    ("system-impersonation", re.compile(r"\b(system|developer)\s*(prompt|message)\s*:", re.I)),
    (
        "authorization-claim",
        re.compile(r"(you\s+are\s+)?(authori[sz]ed|pre-?approved|permitted)\s+to", re.I),
    ),
    ("tool-injection", re.compile(r"<\s*/?\s*(tool_use|function_call|invoke)\b", re.I)),
    ("exfiltration", re.compile(r"\b(curl|wget|fetch)\s+https?://", re.I)),
    (
        "secret-request",
        re.compile(
            r"(reveal|print|output)\s+(your|the)\s+(system\s+)?"
            r"(prompt|instructions|credentials|token)",
            re.I,
        ),
    ),
)

NATURAL_LANGUAGE_FIELDS = frozenset({"description", "summary", "title"})


def _loc(file: str, node: Any, key: str | int | None = None) -> Location:
    line, col = line_of(node, key)
    return Location(file=file, line=line, column=col)


def check_operation_ids(doc: CommentedMap, file: str) -> list[Finding]:
    """FR-V-06 - every operation needs a unique, stable operationId.

    Without one the generator invents a method name from the path, so renaming a path
    silently renames every generated method and breaks each delegate and test that calls it.
    """
    findings: list[Finding] = []
    seen: dict[str, str] = {}

    for path, method, op in iter_operations(doc):
        ptr = pointer("paths", path, method)
        op_id = op.get("operationId")

        if not op_id or not str(op_id).strip():
            suggestion = _suggest_operation_id(method, path)
            findings.append(
                Finding(
                    id="O2J-V-006",
                    rule="operation-id-required",
                    severity="error",
                    message=(
                        f"{method.upper()} {path} has no operationId. The generator will "
                        f"derive an unstable method name from the path, so editing the path "
                        f"renames the generated method and breaks every caller."
                    ),
                    pointer=ptr,
                    location=_loc(file, op),
                    remediation=f"Add `operationId: {suggestion}` to this operation.",
                    auto_fixable=False,
                )
            )
            continue

        op_id = str(op_id)
        if op_id in seen:
            findings.append(
                Finding(
                    id="O2J-V-006",
                    rule="operation-id-duplicate",
                    severity="error",
                    message=(
                        f"operationId {op_id!r} is also used by {seen[op_id]}. Generated "
                        f"method names would collide."
                    ),
                    pointer=ptr,
                    location=_loc(file, op, "operationId"),
                    remediation="Give each operation a globally unique operationId.",
                    auto_fixable=False,
                )
            )
        else:
            seen[op_id] = f"{method.upper()} {path}"

        if not CAMEL_CASE.match(op_id):
            findings.append(
                Finding(
                    id="O2J-V-010",
                    rule="operation-id-camel-case",
                    severity="error",
                    message=(
                        f"operationId {op_id!r} is not camelCase; it becomes a Java "
                        f"method name."
                    ),
                    pointer=ptr,
                    location=_loc(file, op, "operationId"),
                    remediation=f"Rename to `{_to_camel(op_id)}`.",
                    auto_fixable=True,
                )
            )

    return findings


def _suggest_operation_id(method: str, path: str) -> str:
    segments = [s for s in path.split("/") if s and not s.startswith("{")]
    noun = segments[-1] if segments else "resource"
    verb = {
        "get": "get",
        "post": "create",
        "put": "replace",
        "patch": "update",
        "delete": "delete",
    }.get(method, method)
    return _to_camel(f"{verb}-{noun}")


def _to_camel(value: str) -> str:
    parts = re.split(r"[^a-zA-Z0-9]+", value)
    parts = [p for p in parts if p]
    if not parts:
        return "operation"
    head, *rest = parts
    return head[0].lower() + head[1:] + "".join(p[:1].upper() + p[1:] for p in rest)


def check_nullability(doc: CommentedMap, file: str, version: str) -> list[Finding]:
    """FR-V-05 - ``nullable`` and type-union forms must match the declared version.

    In a 3.1 document ``nullable: true`` is not a keyword at all. Tooling ignores it, so the
    generated field is non-nullable while the spec author believes otherwise.
    """
    family = version_family(version)
    findings: list[Finding] = []

    for ptr, schema in iter_schemas(doc):
        has_nullable = "nullable" in schema
        type_value = schema.get("type")
        is_union = isinstance(type_value, CommentedSeq) and "null" in [str(t) for t in type_value]

        if family in ("3.1", "3.2") and has_nullable:
            findings.append(
                Finding(
                    id="O2J-V-005",
                    rule="nullable-representation-mismatch",
                    severity="error",
                    message=(
                        f"`nullable` is not an OpenAPI {family} keyword. It will be ignored, "
                        f"and the generated Java field will not be nullable as intended."
                    ),
                    pointer=f"{ptr}/nullable" if ptr != "#" else "#/nullable",
                    location=_loc(file, schema, "nullable"),
                    remediation=(
                        'Replace with a type union, e.g. `type: ["string", "null"]`.'
                    ),
                    auto_fixable=True,
                )
            )

        if family == "3.0" and is_union:
            findings.append(
                Finding(
                    id="O2J-V-005",
                    rule="type-union-unsupported",
                    severity="error",
                    message=(
                        "A `null` type union is not valid in OpenAPI 3.0. The generated model "
                        "will not treat this field as nullable."
                    ),
                    pointer=f"{ptr}/type",
                    location=_loc(file, schema, "type"),
                    remediation="Use a single `type` plus `nullable: true`.",
                    auto_fixable=True,
                )
            )

    return findings


def check_exclusive_bounds(doc: CommentedMap, file: str, version: str) -> list[Finding]:
    """FR-V-04 - ``exclusiveMinimum``/``exclusiveMaximum`` changed form between 3.0 and 3.1.

    3.0 takes a boolean that modifies ``minimum``; 3.1 takes the bound itself as a number.
    Using the wrong form generates a wrong (or missing) ``@DecimalMin`` constraint, so
    out-of-range values pass validation.
    """
    family = version_family(version)
    findings: list[Finding] = []

    for ptr, schema in iter_schemas(doc):
        for key in ("exclusiveMinimum", "exclusiveMaximum"):
            if key not in schema:
                continue
            value = schema[key]
            is_bool = isinstance(value, bool)

            if family in ("3.1", "3.2") and is_bool:
                paired = "minimum" if key == "exclusiveMinimum" else "maximum"
                bound = schema.get(paired)
                findings.append(
                    Finding(
                        id="O2J-V-005",
                        rule="exclusive-bound-form-mismatch",
                        severity="error",
                        message=(
                            f"`{key}` must be a number in OpenAPI {family}, not a boolean. "
                            f"As a boolean it is ignored and the generated bean-validation "
                            f"constraint will be wrong."
                        ),
                        pointer=f"{ptr}/{key}",
                        location=_loc(file, schema, key),
                        remediation=(
                            f"Write `{key}: {bound}` and remove `{paired}`."
                            if bound is not None
                            else f"Give `{key}` the numeric bound and remove `{paired}`."
                        ),
                        auto_fixable=bound is not None,
                    )
                )

            if family == "3.0" and not is_bool:
                findings.append(
                    Finding(
                        id="O2J-V-005",
                        rule="exclusive-bound-form-mismatch",
                        severity="error",
                        message=(
                            f"`{key}` must be a boolean in OpenAPI 3.0, with the bound given "
                            f"by `minimum`/`maximum`."
                        ),
                        pointer=f"{ptr}/{key}",
                        location=_loc(file, schema, key),
                        remediation=(
                            f"Write `{'minimum' if key == 'exclusiveMinimum' else 'maximum'}: "
                            f"{value}` and `{key}: true`."
                        ),
                        auto_fixable=True,
                    )
                )

    return findings


def check_refs(doc: CommentedMap, file: str, version: str, cfg: O2JConfig) -> list[Finding]:
    """FR-V-04 - $ref must resolve; siblings and remote refs are reported."""
    family = version_family(version)
    findings: list[Finding] = []
    seen: set[int] = set()

    def walk(node: Any, trail: list[str | int]) -> None:
        if id(node) in seen:
            return
        if isinstance(node, CommentedMap):
            seen.add(id(node))
            ref = node.get("$ref")
            if isinstance(ref, str):
                ptr = pointer(*trail)
                findings.extend(_check_one_ref(doc, node, ref, ptr, file, family, cfg))
            for k, v in node.items():
                walk(v, [*trail, str(k)])
        elif isinstance(node, CommentedSeq):
            seen.add(id(node))
            for i, v in enumerate(node):
                walk(v, [*trail, i])

    walk(doc, [])
    return findings


def _check_one_ref(
    doc: CommentedMap,
    node: CommentedMap,
    ref: str,
    ptr: str,
    file: str,
    family: str,
    cfg: O2JConfig,
) -> list[Finding]:
    findings: list[Finding] = []

    if ref.startswith("#/"):
        if resolve_pointer(doc, ref) is None:
            findings.append(
                Finding(
                    id="O2J-V-004",
                    rule="ref-unresolvable",
                    severity="error",
                    message=f"$ref {ref!r} does not resolve within this document.",
                    pointer=ptr,
                    location=_loc(file, node, "$ref"),
                    remediation="Fix the pointer, or define the missing component.",
                    auto_fixable=False,
                )
            )
    elif not cfg.openapi.allow_remote_refs:
        host_allowed = any(ref.startswith(a) for a in cfg.openapi.remote_ref_allowlist)
        if not host_allowed:
            findings.append(
                Finding(
                    id="O2J-V-004",
                    rule="ref-remote-not-allowed",
                    severity="error",
                    message=(
                        f"Remote $ref {ref!r} is not allowed. Generation runs offline, and a "
                        f"remote reference makes the build depend on a network fetch."
                    ),
                    pointer=ptr,
                    location=_loc(file, node, "$ref"),
                    remediation=(
                        "Bundle the reference into this document, or add its prefix to "
                        "`openapi.remote_ref_allowlist`."
                    ),
                    auto_fixable=False,
                )
            )

    siblings = [k for k in node if k != "$ref"]
    if siblings:
        # Legal in 3.1 (JSON Schema 2020-12), ignored by 3.0 tooling. Either way it is worth
        # flagging, because the sibling keywords tend to be the validation constraints and
        # their loss is invisible in the generated code.
        severity = "warn" if family in ("3.1", "3.2") else "error"
        findings.append(
            Finding(
                id="O2J-V-004",
                rule="ref-with-siblings",
                severity=severity,
                message=(
                    f"$ref has sibling keywords ({', '.join(sorted(siblings))}). "
                    + (
                        "Legal in 3.1 but inconsistently honoured by generators, so the "
                        "siblings may be silently dropped."
                        if severity == "warn"
                        else "In 3.0 all siblings of $ref are ignored."
                    )
                ),
                pointer=ptr,
                location=_loc(file, node, "$ref"),
                remediation="Move the sibling keywords into an `allOf` alongside the $ref.",
                auto_fixable=False,
            )
        )

    return findings


def check_response_schemas(doc: CommentedMap, file: str) -> list[Finding]:
    """FR-V-07 - every non-204 success response needs a schema.

    A success response with no schema generates a method returning ``Void``, which is almost
    never what the author meant and is not caught by any schema validator.
    """
    findings: list[Finding] = []

    for path, method, op in iter_operations(doc):
        responses = op.get("responses")
        if not isinstance(responses, CommentedMap):
            continue
        for status, response in responses.items():
            status_str = str(status)
            if not status_str.startswith("2") or status_str in ("204", "205"):
                continue
            if not isinstance(response, CommentedMap):
                continue
            if "$ref" in response:
                continue
            content = response.get("content")
            has_schema = isinstance(content, CommentedMap) and any(
                isinstance(media, CommentedMap) and "schema" in media
                for media in content.values()
            )
            if not has_schema:
                findings.append(
                    Finding(
                        id="O2J-V-007",
                        rule="response-schema-required",
                        severity="error",
                        message=(
                            f"{method.upper()} {path} response {status_str} has no schema; the "
                            f"generated method would return Void."
                        ),
                        pointer=pointer("paths", path, method, "responses", status_str),
                        location=_loc(file, response),
                        remediation=(
                            "Add `content: {application/json: {schema: {$ref: ...}}}`, or use "
                            "204 if the response genuinely has no body."
                        ),
                        auto_fixable=False,
                    )
                )

    return findings


def check_security(doc: CommentedMap, file: str, cfg: O2JConfig) -> list[Finding]:
    """FR-V-08 - operations must declare security unless explicitly public."""
    if not cfg.validation.require_security:
        return []

    findings: list[Finding] = []
    global_security = doc.get("security")
    components = doc.get("components")
    schemes = (
        components.get("securitySchemes")
        if isinstance(components, CommentedMap)
        else None
    )

    if not schemes:
        findings.append(
            Finding(
                id="O2J-V-008",
                rule="security-schemes-required",
                severity="error",
                message="No `components.securitySchemes` declared; no endpoint can be secured.",
                pointer="#/components",
                location=_loc(file, doc, "components"),
                remediation="Declare at least one security scheme, e.g. an OAuth2 flow.",
                auto_fixable=False,
            )
        )

    for path, method, op in iter_operations(doc):
        op_security = op.get("security")
        # An explicit empty list means "deliberately public" and is honoured.
        if op_security is not None:
            continue
        if global_security:
            continue
        findings.append(
            Finding(
                id="O2J-V-008",
                rule="security-required",
                severity="error",
                message=(
                    f"{method.upper()} {path} declares no security and there is no global "
                    f"default, so it generates an unauthenticated endpoint."
                ),
                pointer=pointer("paths", path, method),
                location=_loc(file, op),
                remediation=(
                    "Add a `security` requirement, or `security: []` to mark it deliberately "
                    "public."
                ),
                auto_fixable=False,
            )
        )

    return findings


def check_inline_schemas(doc: CommentedMap, file: str, cfg: O2JConfig) -> list[Finding]:
    """FR-V-09 - inline object schemas generate unstable class names.

    An inline object becomes ``InlineObject1``, ``InlineObject2`` and so on. The numbering
    depends on document order, so inserting an unrelated operation renumbers existing
    classes and silently changes the generated API.
    """
    findings: list[Finding] = []
    budget = cfg.openapi.max_inline_schemas
    count = 0

    for path, method, op in iter_operations(doc):
        locations: list[tuple[str, Any]] = []

        body = op.get("requestBody")
        if isinstance(body, CommentedMap):
            locations.extend(
                _inline_media_schemas(body, pointer("paths", path, method, "requestBody"))
            )

        responses = op.get("responses")
        if isinstance(responses, CommentedMap):
            for status, response in responses.items():
                if isinstance(response, CommentedMap):
                    locations.extend(
                        _inline_media_schemas(
                            response,
                            pointer("paths", path, method, "responses", str(status)),
                        )
                    )

        for ptr, schema in locations:
            count += 1
            if count > budget:
                findings.append(
                    Finding(
                        id="O2J-V-009",
                        rule="inline-schema-not-allowed",
                        severity="error",
                        message=(
                            f"Inline object schema at {method.upper()} {path}. It generates an "
                            f"ordinal class name (InlineObject-style) that renumbers when the "
                            f"document is edited."
                        ),
                        pointer=ptr,
                        location=_loc(file, schema),
                        remediation=(
                            "Move the schema into `components.schemas` with a meaningful name "
                            "and reference it with $ref."
                        ),
                        auto_fixable=False,
                    )
                )

    return findings


def _inline_media_schemas(holder: CommentedMap, base: str) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    content = holder.get("content")
    if not isinstance(content, CommentedMap):
        return out
    for media_type, media in content.items():
        if not isinstance(media, CommentedMap):
            continue
        schema = media.get("schema")
        if not isinstance(schema, CommentedMap):
            continue
        if "$ref" in schema:
            continue
        # Only object-shaped schemas get a generated class; primitives and arrays of $ref
        # are fine inline.
        if schema.get("type") == "object" or "properties" in schema:
            out.append((f"{base}/content/{media_type}/schema".replace("//", "/"), schema))
    return out


def check_allof_depth(doc: CommentedMap, file: str, cfg: O2JConfig) -> list[Finding]:
    """FR-V-05 / FR-N-08 - deep allOf composition generates unusable Java."""
    findings: list[Finding] = []
    limit = cfg.openapi.max_allof_depth

    def depth(node: Any, seen: frozenset[int] = frozenset()) -> int:
        if not isinstance(node, CommentedMap) or id(node) in seen:
            return 0
        seen = seen | {id(node)}
        all_of = node.get("allOf")
        if not isinstance(all_of, CommentedSeq):
            return 0
        child_depths = [0]
        for member in all_of:
            target = member
            if isinstance(member, CommentedMap) and isinstance(member.get("$ref"), str):
                target = resolve_pointer(doc, member["$ref"]) or member
            child_depths.append(depth(target, seen))
        return 1 + max(child_depths)

    components = doc.get("components")
    schemas = components.get("schemas") if isinstance(components, CommentedMap) else None
    if isinstance(schemas, CommentedMap):
        for name, schema in schemas.items():
            d = depth(schema)
            if d > limit:
                findings.append(
                    Finding(
                        id="O2J-V-005",
                        rule="allof-depth-exceeded",
                        severity="warn",
                        message=(
                            f"Schema {name!r} composes allOf {d} levels deep (budget {limit}). "
                            f"It will be flattened during normalization; the generated class "
                            f"will not reflect the composition structure."
                        ),
                        pointer=pointer("components", "schemas", str(name)),
                        location=_loc(file, schemas, name),
                        remediation="Flatten the composition in the spec, or raise the budget.",
                        auto_fixable=False,
                    )
                )

    return findings


def check_injection(doc: CommentedMap, file: str) -> list[Finding]:
    """FR-V-13 - detect text written to steer an AI agent reading the spec.

    The spec reaches Copilot agents, and ``description`` is free text an upstream team or an
    attacker controls. Such text is data; this check makes sure a human is told it is there.
    """
    findings: list[Finding] = []
    seen: set[int] = set()

    def walk(node: Any, trail: list[str | int]) -> None:
        if id(node) in seen:
            return
        if isinstance(node, CommentedMap):
            seen.add(id(node))
            for k, v in node.items():
                key = str(k)
                if key in NATURAL_LANGUAGE_FIELDS and isinstance(v, str):
                    for name, pattern in INJECTION_PATTERNS:
                        match = pattern.search(v)
                        if match:
                            findings.append(
                                Finding(
                                    id="O2J-V-013",
                                    rule=f"prompt-injection-suspected:{name}",
                                    severity="warn",
                                    message=(
                                        f"The `{key}` field contains text that reads as an "
                                        f"instruction to an AI agent "
                                        f"({match.group(0)[:60]!r}). It is documentation data "
                                        f"and must never be followed as instruction."
                                    ),
                                    pointer=pointer(*trail, key),
                                    location=_loc(file, node, k),
                                    remediation=(
                                        "Remove the text. `o2j normalize` redacts it before any "
                                        "agent reads the spec, but the upstream source should "
                                        "be corrected."
                                    ),
                                    auto_fixable=True,
                                )
                            )
                            break
                walk(v, [*trail, key])
        elif isinstance(node, CommentedSeq):
            seen.add(id(node))
            for i, v in enumerate(node):
                walk(v, [*trail, i])

    walk(doc, [])
    return findings


def check_openapi_32_constructs(doc: CommentedMap, file: str, version: str) -> list[Finding]:
    """Report 3.2-only constructs at validation time, before normalization decides their fate.

    Reported as info here: the decision about whether a loss is acceptable belongs to
    normalization, which classifies each construct and fails on a load-bearing one.
    """
    if version_family(version) != "3.2":
        return []

    findings: list[Finding] = []
    paths = doc.get("paths")
    if isinstance(paths, CommentedMap):
        for path, item in paths.items():
            if isinstance(item, CommentedMap) and "additionalOperations" in item:
                findings.append(
                    Finding(
                        id="O2J-V-002",
                        rule="openapi-32-additional-operations",
                        severity="info",
                        message=(
                            f"{path} uses `additionalOperations`, which has no OpenAPI 3.1 "
                            f"equivalent. Normalization will classify this as a load-bearing "
                            f"loss and stop the run."
                        ),
                        pointer=pointer("paths", str(path), "additionalOperations"),
                        location=_loc(file, item, "additionalOperations"),
                        remediation=(
                            "Express the operation with a standard HTTP method, or wait for "
                            "swagger-parser 3.2 support (swagger-api/swagger-parser#2248)."
                        ),
                        auto_fixable=False,
                    )
                )
    return findings


def run_all(doc: CommentedMap, file: str, version: str, cfg: O2JConfig) -> list[Finding]:
    """Run every semantic check and return the combined findings."""
    return [
        *check_operation_ids(doc, file),
        *check_nullability(doc, file, version),
        *check_exclusive_bounds(doc, file, version),
        *check_refs(doc, file, version, cfg),
        *check_response_schemas(doc, file),
        *check_security(doc, file, cfg),
        *check_inline_schemas(doc, file, cfg),
        *check_allof_depth(doc, file, cfg),
        *check_injection(doc, file),
        *check_openapi_32_constructs(doc, file, version),
    ]
