"""S3 Plan - the operation inventory every downstream agent reads.

The important output here is ``requiredTests``. Asking an agent to "write thorough tests"
produces high line coverage and weak assertions, because exercising a line is easy and
asserting the right outcome is not. Deriving the mandatory cases from each operation's shape
turns coverage into something checkable: the test author is told which cases must exist, and
``o2j verify --contract`` can confirm each one does.

See SPECIFICATION.md Sections 10.3 and 12.2.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from o2j import ems
from o2j.config import O2JConfig
from o2j.errors import O2JError
from o2j.models import (
    GenerationPlan,
    PlannedOperation,
    PlannedResource,
    PlanTarget,
    RiskScore,
)
from o2j.specdoc import (
    detect_version,
    iter_operations,
    load_spec,
    pointer,
    resolve_pointer,
    sha256_file,
)
from o2j.workdir import WorkDir

IDEMPOTENCY_HEADER = "idempotency-key"
CONCURRENCY_HEADERS = frozenset({"if-match", "if-none-match"})
PAGINATION_PARAMS = frozenset({"page", "size", "limit", "cursor", "offset", "pageToken"})


def plan(
    cfg: O2JConfig,
    canonical_path: str | Path | None = None,
    work: WorkDir | None = None,
) -> GenerationPlan:
    """Build the generation plan from the canonical spec."""
    work = work or WorkDir()
    path = (
        Path(canonical_path)
        if canonical_path is not None
        else work.path("normalize", "spec.canonical.yaml", create=False)
    )
    if not path.is_file():
        raise O2JError(
            f"canonical spec not found: {path}",
            hint="run `o2j normalize` first",
        )

    doc = load_spec(path)
    operations = iter_operations(doc)
    if not operations:
        raise O2JError(
            "the specification declares no operations, so there is nothing to generate"
        )

    global_security = doc.get("security")
    buckets: dict[str, list[PlannedOperation]] = {}
    parameters: dict[str, list[CommentedMap]] = {}

    for op_path, method, op in operations:
        tag = _primary_tag(op)
        planned = _plan_operation(doc, op_path, method, op, global_security)
        buckets.setdefault(tag, []).append(planned)
        parameters[planned.operation_id] = _all_parameters(doc, op, op_path)

    resources = [
        PlannedResource(
            tag=tag,
            api_interface=f"{_pascal(tag)}Api",
            delegate=f"{_pascal(tag)}ApiDelegateImpl",
            service=f"{_singular_pascal(tag)}Service",
            operations=ops,
        )
        for tag, ops in sorted(buckets.items())
    ]

    base_path = _base_path(doc)
    flat = [op for r in resources for op in r.operations]
    ems_routes = ems.plan_routes(cfg, doc, flat, base_path, parameters)
    by_id = {op.operation_id: op for op in flat}
    for route in ems_routes:
        target = by_id[route.operation_id]
        target.required_tests += [
            c for c in ems.required_cases(route) if c not in target.required_tests
        ]

    return GenerationPlan(
        spec_hash=sha256_file(path),
        spec_version=detect_version(doc),
        original_version=detect_version(doc),
        target=PlanTarget(
            java_version=cfg.target.java_version,
            spring_profile=cfg.target.spring_profile,
            spring_boot_version=cfg.spring_boot_version,
            build_tool=cfg.target.build_tool,
            web_stack=cfg.target.web_stack,
            java_tier=cfg.target.tier,
        ),
        base_package=cfg.service.base_package,
        base_path=base_path,
        security_schemes=_security_schemes(doc),
        resources=resources,
        risk_score=_score_risk(doc, resources),
        ems_routes=ems_routes,
    )


def _plan_operation(
    doc: CommentedMap,
    path: str,
    method: str,
    op: CommentedMap,
    global_security: Any,
) -> PlannedOperation:
    raw_responses = op.get("responses")
    responses = raw_responses if isinstance(raw_responses, CommentedMap) else CommentedMap()
    statuses = [str(s) for s in responses]
    success = _success_status(statuses, method)
    errors = sorted({int(s) for s in statuses if s[:1] in ("4", "5") and s.isdigit()})

    params = _all_parameters(doc, op, path)
    header_names = {
        str(p.get("name", "")).lower() for p in params if p.get("in") == "header"
    }
    param_names = {str(p.get("name", "")) for p in params}

    security = _security_scopes(op, global_security)
    requirements = _security_requirements(op, global_security)
    request_type = _type_name(doc, op.get("requestBody"))
    response_type = _type_name(doc, responses.get(str(success)))

    operation = PlannedOperation(
        operation_id=str(op.get("operationId") or f"{method}{_pascal(path)}"),
        method=method.upper(),
        path=path,
        pointer=pointer("paths", path, method),
        request_type=request_type,
        response_type=response_type,
        success_status=success,
        error_statuses=errors,
        security=security,
        security_requirements=requirements,
        idempotent=IDEMPOTENCY_HEADER in header_names,
        paginated=bool(param_names & PAGINATION_PARAMS),
        optimistic_concurrency=bool(header_names & CONCURRENCY_HEADERS),
        enum_fields=_enum_fields(doc, op),
        risk_factors=_risk_factors(doc, op),
    )
    operation.required_tests = derive_required_tests(operation, params)
    return operation


def derive_required_tests(
    op: PlannedOperation, params: list[CommentedMap] | None = None
) -> list[str]:
    """Derive the mandatory test cases for one operation (SPECIFICATION.md Section 12.2).

    Each entry is ``<layer>:<case>``. The test-author agent implements every entry, and the
    contract verifier checks that a matching test exists, so this list is the mechanism that
    makes the coverage requirement meaningful rather than aspirational.
    """
    params = params or []
    tests: list[str] = ["unit:happy-path", "slice:happy-path-status", "contract:response-schema"]

    if op.request_type:
        tests += [
            "unit:validation-failure",
            "slice:400-problem-details",
            "slice:malformed-json-400",
        ]

    # A path parameter cannot be "missing": leaving the segment out is a different route. Only
    # query, header and cookie parameters can be omitted from an otherwise valid request.
    has_required_param = any(
        p.get("in") in ("query", "header", "cookie") and p.get("required") for p in params
    )
    if has_required_param:
        tests.append("slice:missing-required-param-400")
    # A wrong type is only observable when the parameter is not a free-form string.
    if any(p.get("in") in ("path", "query") and is_typed_param(p) for p in params):
        tests.append("slice:wrong-param-type-400")

    if 404 in op.error_statuses:
        tests.append("slice:404-problem-details")

    if op.method == "POST" and op.success_status == 201:
        tests.append("slice:201-location-header")
    if 409 in op.error_statuses:
        tests.append("unit:conflict-409")

    if op.idempotent:
        tests.append("slice:idempotent-replay")

    # Only cases the generated service can actually exhibit: an operation that also allows
    # anonymous access cannot answer 401, and one that needs no scope cannot answer 403.
    if op.requires_authentication:
        tests.append("slice:unauthenticated-401")
    if op.requires_scopes:
        tests.append("slice:wrong-scope-403")

    if op.paginated:
        tests += [
            "slice:pagination-first-page",
            "slice:pagination-last-page",
            "slice:pagination-empty",
            "slice:pagination-beyond-end",
            "slice:invalid-page-size-400",
        ]

    if op.optimistic_concurrency:
        tests.append("slice:stale-if-match-412")

    if op.method in ("POST", "PUT", "PATCH", "DELETE"):
        tests += ["unit:repository-interaction-captured", "unit:downstream-failure-mapped"]
        if 500 in op.error_statuses:
            tests.append("unit:downstream-timeout-mapped")

    for field in op.enum_fields:
        tests.append(f"unit:enum-all-values:{field}")
    if op.enum_fields:
        tests.append("slice:unknown-enum-value-400")

    # Stable order, no duplicates: the list is compared across runs by the contract verifier.
    seen: set[str] = set()
    ordered: list[str] = []
    for t in tests:
        if t not in seen:
            seen.add(t)
            ordered.append(t)
    return ordered


TYPED_STRING_FORMATS = frozenset({"uuid", "date", "date-time"})


def is_typed_param(param: Any) -> bool:
    """Whether a parameter binds to a Java type that rejects arbitrary text."""
    schema = param.get("schema") if hasattr(param, "get") else None
    if schema is None or not hasattr(schema, "get"):
        return False
    kind = schema.get("type")
    if isinstance(kind, (list, CommentedSeq)):
        kind = next((k for k in kind if k != "null"), None)
    if kind in ("integer", "number", "boolean"):
        return True
    return kind == "string" and schema.get("format") in TYPED_STRING_FORMATS


def _all_parameters(doc: CommentedMap, op: CommentedMap, path: str) -> list[CommentedMap]:
    """Operation parameters plus the path item's shared parameters, with $ref resolved."""
    collected: list[CommentedMap] = []
    paths = doc.get("paths")
    item = paths.get(path) if isinstance(paths, CommentedMap) else None

    for source in (item, op):
        if not isinstance(source, CommentedMap):
            continue
        params = source.get("parameters")
        if not isinstance(params, CommentedSeq):
            continue
        for param in params:
            resolved = param
            if isinstance(param, CommentedMap) and isinstance(param.get("$ref"), str):
                target = resolve_pointer(doc, param["$ref"])
                if isinstance(target, CommentedMap):
                    resolved = target
            if isinstance(resolved, CommentedMap):
                collected.append(resolved)
    return collected


def _primary_tag(op: CommentedMap) -> str:
    tags = op.get("tags")
    if isinstance(tags, CommentedSeq) and tags:
        return str(tags[0])
    return "default"


def _success_status(statuses: list[str], method: str) -> int:
    numeric = sorted(int(s) for s in statuses if s.isdigit() and s.startswith("2"))
    if numeric:
        return numeric[0]
    if "default" in statuses:
        return 200
    return 204 if method == "delete" else 200


def _security_scopes(op: CommentedMap, global_security: Any) -> list[str]:
    requirement = op.get("security")
    if requirement is None:
        requirement = global_security
    if not isinstance(requirement, CommentedSeq):
        return []
    scopes: list[str] = []
    for entry in requirement:
        if not isinstance(entry, CommentedMap):
            continue
        for scheme, values in entry.items():
            if isinstance(values, CommentedSeq) and values:
                scopes.extend(f"{scheme}:{v}" for v in values)
            else:
                scopes.append(str(scheme))
    return sorted(set(scopes))


def _security_requirements(
    op: CommentedMap, global_security: Any
) -> list[dict[str, list[str]]]:
    """The effective requirement with its OR-of-AND structure intact.

    Flattening loses the difference between "scope a OR scope b" and "scope a AND scope b",
    which is the difference between two different authorization rules.
    """
    requirement = op.get("security")
    if requirement is None:
        requirement = global_security
    if not isinstance(requirement, CommentedSeq):
        return []
    out: list[dict[str, list[str]]] = []
    for entry in requirement:
        if not isinstance(entry, CommentedMap):
            continue
        out.append(
            {
                str(scheme): sorted(str(v) for v in values)
                if isinstance(values, CommentedSeq)
                else []
                for scheme, values in sorted(entry.items(), key=lambda kv: str(kv[0]))
            }
        )
    return out


def _security_schemes(doc: CommentedMap) -> dict[str, str]:
    components = doc.get("components")
    schemes = components.get("securitySchemes") if isinstance(components, CommentedMap) else None
    if not isinstance(schemes, CommentedMap):
        return {}
    out: dict[str, str] = {}
    for name, scheme in schemes.items():
        if isinstance(scheme, CommentedMap) and isinstance(scheme.get("$ref"), str):
            target = resolve_pointer(doc, scheme["$ref"])
            scheme = target if isinstance(target, CommentedMap) else scheme
        if not isinstance(scheme, CommentedMap):
            continue
        kind = str(scheme.get("type", ""))
        if kind == "http":
            kind = f"http:{str(scheme.get('scheme', '')).lower()}"
        out[str(name)] = kind
    return dict(sorted(out.items()))


def _base_path(doc: CommentedMap) -> str:
    """The path openapi-generator prefixes onto every controller mapping.

    It takes the first server URL, substitutes each server variable's default, and keeps the
    path. Security rules have to match the same prefix or they match nothing.
    """
    servers = doc.get("servers")
    if not isinstance(servers, CommentedSeq) or not servers:
        return ""
    first = servers[0]
    if not isinstance(first, CommentedMap):
        return ""
    url = str(first.get("url", ""))
    variables = first.get("variables")
    if isinstance(variables, CommentedMap):
        for name, spec_var in variables.items():
            default = spec_var.get("default", "") if isinstance(spec_var, CommentedMap) else ""
            url = url.replace("{" + str(name) + "}", str(default))
    path = urlparse(url).path if "://" in url else url
    path = path.rstrip("/")
    return path if path.startswith("/") or not path else "/" + path


def _type_name(doc: CommentedMap, holder: Any) -> str | None:
    """Best-effort Java type name for a request body or response."""
    if not isinstance(holder, CommentedMap):
        return None
    if isinstance(holder.get("$ref"), str):
        target = resolve_pointer(doc, holder["$ref"])
        if isinstance(target, CommentedMap):
            holder = target
    content = holder.get("content")
    if not isinstance(content, CommentedMap):
        return None
    for media in content.values():
        if not isinstance(media, CommentedMap):
            continue
        schema = media.get("schema")
        if not isinstance(schema, CommentedMap):
            continue
        ref = schema.get("$ref")
        if isinstance(ref, str):
            return ref.rsplit("/", 1)[-1]
        if schema.get("type") == "array":
            items = schema.get("items")
            if isinstance(items, CommentedMap) and isinstance(items.get("$ref"), str):
                return f"List<{items['$ref'].rsplit('/', 1)[-1]}>"
            return "List<Object>"
    return None


def _enum_fields(doc: CommentedMap, op: CommentedMap) -> list[str]:
    """Names of enum-typed fields reachable from this operation's request body."""
    body = op.get("requestBody")
    if not isinstance(body, CommentedMap):
        return []
    content = body.get("content")
    if not isinstance(content, CommentedMap):
        return []

    fields: list[str] = []
    for media in content.values():
        if not isinstance(media, CommentedMap):
            continue
        schema = media.get("schema")
        if isinstance(schema, CommentedMap) and isinstance(schema.get("$ref"), str):
            schema = resolve_pointer(doc, schema["$ref"])
        if not isinstance(schema, CommentedMap):
            continue
        props = schema.get("properties")
        if not isinstance(props, CommentedMap):
            continue
        for name, prop in props.items():
            target = prop
            if isinstance(prop, CommentedMap) and isinstance(prop.get("$ref"), str):
                target = resolve_pointer(doc, prop["$ref"]) or prop
            if isinstance(target, CommentedMap) and "enum" in target:
                fields.append(str(name))
    return sorted(set(fields))


def _risk_factors(doc: CommentedMap, op: CommentedMap) -> list[str]:
    """Shapes that generate Java a human should review before building logic on it."""
    factors: list[str] = []

    seen: set[int] = set()

    def inspect(schema: Any, depth: int = 0) -> None:
        if not isinstance(schema, CommentedMap) or depth > 8 or id(schema) in seen:
            return
        seen.add(id(schema))

        if isinstance(schema.get("$ref"), str):
            inspect(resolve_pointer(doc, schema["$ref"]), depth + 1)
            return
        if "oneOf" in schema or "anyOf" in schema:
            factors.append("polymorphic-schema")
        if "discriminator" in schema:
            factors.append("discriminator")
        if "allOf" in schema:
            factors.append("composed-schema")
        if schema.get("additionalProperties") not in (None, False):
            factors.append("free-form-map")

        # Descend through composition as well as properties. A polymorphic field nested under
        # an allOf is the case most worth flagging, and walking `properties` alone misses it
        # entirely because composed schemas hold their properties in the members.
        for keyword in ("allOf", "oneOf", "anyOf"):
            members = schema.get(keyword)
            if isinstance(members, CommentedSeq):
                for member in members:
                    inspect(member, depth + 1)

        props = schema.get("properties")
        if isinstance(props, CommentedMap):
            for value in props.values():
                inspect(value, depth + 1)

        items = schema.get("items")
        if isinstance(items, CommentedMap):
            inspect(items, depth + 1)

    body = op.get("requestBody")
    if isinstance(body, CommentedMap):
        content = body.get("content")
        if isinstance(content, CommentedMap):
            for media in content.values():
                if isinstance(media, CommentedMap):
                    inspect(media.get("schema"))

    return sorted(set(factors))


def _score_risk(doc: CommentedMap, resources: list[PlannedResource]) -> RiskScore:
    """A coarse score that tells the orchestrator whether to pause for human review.

    Weighted towards shapes that generate misleading Java: a polymorphic request body costs
    more review attention than ten extra plain operations.
    """
    operations = [op for r in resources for op in r.operations]
    drivers: list[str] = []
    score = 0

    polymorphic = sum(1 for op in operations if "polymorphic-schema" in op.risk_factors)
    composed = sum(1 for op in operations if "composed-schema" in op.risk_factors)
    freeform = sum(1 for op in operations if "free-form-map" in op.risk_factors)

    if polymorphic:
        score += polymorphic * 8
        drivers.append(f"{polymorphic} polymorphic request schemas")
    if composed:
        score += composed * 3
        drivers.append(f"{composed} composed (allOf) schemas")
    if freeform:
        score += freeform * 5
        drivers.append(f"{freeform} free-form map schemas")

    if len(operations) > 50:
        score += 15
        drivers.append(f"{len(operations)} operations")
    elif len(operations) > 20:
        score += 8
        drivers.append(f"{len(operations)} operations")

    components = doc.get("components")
    schemas = components.get("schemas") if isinstance(components, CommentedMap) else None
    schema_count = len(schemas) if isinstance(schemas, CommentedMap) else 0
    if schema_count > 100:
        score += 10
        drivers.append(f"{schema_count} schemas")

    band: Literal["low", "medium", "high"] = "low"
    if score >= 40:
        band = "high"
    elif score >= 15:
        band = "medium"

    return RiskScore(value=min(score, 100), band=band, drivers=drivers)


def _pascal(value: str) -> str:
    parts = [p for p in re.split(r"[^a-zA-Z0-9]+", value) if p and not p.startswith("{")]
    return "".join(p[:1].upper() + p[1:] for p in parts) or "Default"


def _singular_pascal(value: str) -> str:
    name = _pascal(value)
    # Crude but predictable; a wrong singular is a cosmetic class-name issue, whereas an
    # unpredictable one would churn between runs.
    if name.endswith("ies"):
        return name[:-3] + "y"
    if name.endswith("ses") or name.endswith("xes"):
        return name[:-2]
    if name.endswith("s") and not name.endswith("ss"):
        return name[:-1]
    return name
