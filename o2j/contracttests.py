"""Derive the contract-level test cases the spec fully determines.

These cases need no business knowledge: given the spec, the right answer is already known.
A valid request reaches the delegate and comes back with the declared status; a body missing
a required field, malformed JSON, an unknown enum value, a missing required parameter or a
wrongly-typed one is refused with 400 Problem Details before the delegate is called; a
NotFoundException becomes 404; a page size past its maximum is refused. o2j generates them,
so they cannot drift from the spec and nobody has to remember to write them.

What a spec cannot determine - the business rule behind ``unit:happy-path``, what an empty
page looks like, which duplicate is a 409 - is left to people, as disabled skeletons.

The delegate is replaced by a stub (``O2jDelegateStub``) so each case isolates the contract
layer: routing, binding, Bean Validation, Jackson, security and the error advice.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from o2j import security
from o2j.models import GenerationPlan, PlannedOperation
from o2j.samples import SampleError, Sampler
from o2j.stages.plan import IDEMPOTENCY_HEADER, is_typed_param

JSON_TYPES = ("application/json", "application/problem+json")
REPLAYED_HEADER = "Idempotent-Replayed"  # set by the generated IdempotencyFilter
PAGE_SIZE_NAMES = ("size", "limit", "pageSize", "page_size", "perPage", "per_page")
UNKNOWN_ENUM = "O2J_UNKNOWN_VALUE"
NOT_A_NUMBER = "o2j-not-a-number"
EMS_CORRELATION = "o2j-correlation-1"
TOKEN_LIKE = re.compile(r"\{\{\{?\s*[#^/]?\s*[\w.]+\s*\}?\}\}")


@dataclass
class ContractCase:
    method_name: str
    markers: list[str]
    lines: list[str]


@dataclass
class OperationCases:
    operation_id: str
    cases: list[ContractCase] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # human-readable reasons


@dataclass
class _Request:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: Any | None
    media: str | None
    auth: str | None  # Java post-processor expression, or None for anonymous


def derive(plan: GenerationPlan, spec: dict[str, Any]) -> dict[str, OperationCases]:
    """Contract cases per operationId."""
    sampler = Sampler(spec)
    rules = {r.operation_id: r for r in security.rules(plan)}
    out: dict[str, OperationCases] = {}
    for op in plan.operations:
        out[op.operation_id] = _derive_operation(op, spec, sampler, plan, rules[op.operation_id])
    return out


def _derive_operation(
    op: PlannedOperation,
    spec: dict[str, Any],
    sampler: Sampler,
    plan: GenerationPlan,
    rule: security.SecurityRule,
) -> OperationCases:
    result = OperationCases(op.operation_id)
    if rule.expression == "denyAll()":
        result.skipped.append("security scheme not wired, so no request reaches the controller")
        return result

    raw_op = spec["paths"][op.path][op.method.lower()]
    params = _parameters(spec, sampler, op.path, raw_op)
    try:
        request = _valid_request(op, plan, sampler, raw_op, params)
    except SampleError as exc:
        result.skipped.append(f"no valid request could be synthesized: {exc}")
        return result

    required = set(op.required_tests)
    ident = security.java_identifier(op.operation_id)
    oid = op.operation_id
    idempotency_key = next(
        (p for p in params
         if p["in"] == "header" and p["name"].lower() == IDEMPOTENCY_HEADER and "value" in p),
        None,
    )

    def add(case: str, suffix: str, lines: list[str], extra: list[str] | None = None) -> None:
        markers = [op.marker(c) for c in [case, *(extra or [])] if c in required]
        if markers:
            if idempotency_key is not None:
                # One key per case: the service's IdempotencyFilter would otherwise replay one
                # case's stored response into the next.
                lines = _with_distinct_key(lines, idempotency_key, f"{oid}:{suffix}")
            result.cases.append(ContractCase(f"{ident}_{suffix}", markers, lines))

    # Happy path: the declared status, the declared content type, exactly one delegate call.
    success_media, success_schema = _response_content(spec, sampler, raw_op, op.success_status)
    stub_body: str | None = None
    if success_schema is not None:
        try:
            stub_body = json.dumps(sampler.sample(success_schema, for_request=False))
        except SampleError as exc:
            result.skipped.append(f"happy path: no valid response sample ({exc})")
    if success_schema is None or stub_body is not None:
        lines = [_stub_respond(oid, op.success_status, stub_body)]
        lines += _perform(request, [f"status().is({op.success_status})"] + (
            [f"content().contentTypeCompatibleWith(MediaType.parseMediaType({_j(success_media)}))"]
            if success_media and stub_body is not None
            else []
        ))
        lines.append(f"assertThat(stub.calls({_j(oid)})).isEqualTo(1);")
        add("slice:happy-path-status", "validRequest_returnsDeclaredStatus", lines,
            ["contract:response-schema"])

        # Idempotency-Key: the same request twice reaches the delegate once and is replayed.
        if idempotency_key is not None:
            expect = [f"status().is({op.success_status})"]
            lines = [_stub_respond(oid, op.success_status, stub_body)]
            lines += _perform(request, expect)
            lines += _perform(request, [*expect, f'header().string("{REPLAYED_HEADER}", "true")'])
            lines.append(f"assertThat(stub.calls({_j(oid)})).isEqualTo(1);")
            add("slice:idempotent-replay",
                "sameIdempotencyKey_replaysWithoutSecondCall", lines)

    refused = 'jsonPath("$.status").value(400)'

    # Request-body cases.
    if request.body is not None:
        missing = _without_required(sampler, raw_op, spec, request.body)
        if missing is not None:
            name, body = missing
            lines = _perform(
                _replace(request, body=body),
                ["status().isBadRequest()", _problem_json(), refused,
                 'jsonPath("$.type").value(endsWith("/validation-failed"))',
                 'jsonPath("$.errors").isNotEmpty()'],
            )
            lines.append(f"assertThat(stub.calls({_j(oid)})).isZero();")
            add("slice:400-problem-details", f"missing_{security.java_identifier(name)}_is400",
                lines)

        lines = _perform(
            _replace(request, body=_RAW("{")),
            ["status().isBadRequest()", _problem_json(), refused,
             'jsonPath("$.type").value(endsWith("/malformed-request"))'],
        )
        lines.append(f"assertThat(stub.calls({_j(oid)})).isZero();")
        add("slice:malformed-json-400", "malformedJson_is400", lines)

    # Unknown enum value, in the body first, then in a query parameter.
    enum_request = _with_unknown_enum(sampler, request, params)
    if enum_request is not None:
        lines = _perform(enum_request, ["status().isBadRequest()", _problem_json(), refused])
        lines.append(f"assertThat(stub.calls({_j(oid)})).isZero();")
        add("slice:unknown-enum-value-400", "unknownEnumValue_is400", lines)

    # A required query/header parameter left out.
    omittable = [p for p in params if p["in"] in ("query", "header") and p.get("required")]
    if omittable:
        target = omittable[0]
        bucket = "query" if target["in"] == "query" else "headers"
        reduced = {k: v for k, v in getattr(request, bucket).items() if k != target["name"]}
        lines = _perform(
            _replace(request, **{bucket: reduced}),
            ["status().isBadRequest()", _problem_json(), refused],
        )
        lines.append(f"assertThat(stub.calls({_j(oid)})).isZero();")
        add("slice:missing-required-param-400", "missingRequiredParameter_is400", lines)

    # A typed path or query parameter given text.
    typed = [p for p in params if p["in"] in ("path", "query") and is_typed_param(p)]
    if typed:
        target = typed[0]
        if target["in"] == "path":
            bad = _replace(request, path=_fill_path(plan.base_path, op.path, {
                **{p["name"]: p["value"] for p in params if p["in"] == "path"},
                target["name"]: NOT_A_NUMBER,
            }))
        else:
            bad = _replace(request, query={**request.query, target["name"]: NOT_A_NUMBER})
        lines = _perform(bad, ["status().isBadRequest()", _problem_json(), refused])
        lines.append(f"assertThat(stub.calls({_j(oid)})).isZero();")
        add("slice:wrong-param-type-400", "wrongParameterType_is400", lines)

    # Domain NotFoundException is mapped to 404 Problem Details.
    if 404 in op.error_statuses:
        lines = [f'stub.fail({_j(oid)}, new NotFoundException("resource", "o2j-test"));']
        lines += _perform(request, [
            "status().isNotFound()", _problem_json(), 'jsonPath("$.status").value(404)',
            'jsonPath("$.type").value(endsWith("/not-found"))',
        ])
        add("slice:404-problem-details", "notFound_is404ProblemDetails", lines)

    # A page size past its declared maximum.
    if op.paginated:
        size = next(
            (p for p in params if p["in"] == "query" and p["name"] in PAGE_SIZE_NAMES
             and isinstance(p.get("schema", {}).get("maximum"), (int, float))),
            None,
        )
        if size is not None:
            too_big = str(int(size["schema"]["maximum"]) + 1)
            lines = _perform(
                _replace(request, query={**request.query, size["name"]: too_big}),
                ["status().isBadRequest()", _problem_json(), refused],
            )
            lines.append(f"assertThat(stub.calls({_j(oid)})).isZero();")
            add("slice:invalid-page-size-400", "pageSizeAboveMaximum_is400", lines)

    return result


# ---- request construction ---------------------------------------------------------------


class _RAW(str):
    """A request body sent verbatim rather than JSON-encoded."""


def _parameters(
    spec: dict[str, Any], sampler: Sampler, path: str, raw_op: dict[str, Any]
) -> list[dict[str, Any]]:
    collected: dict[tuple[str, str], dict[str, Any]] = {}
    for source in (spec["paths"][path], raw_op):
        for param in source.get("parameters", []) or []:
            resolved = sampler.resolve(param) if "$ref" in param else param
            entry = dict(resolved)
            if "schema" in entry:
                entry["schema"] = sampler.resolve(entry["schema"])
            collected[(entry["name"], entry["in"])] = entry
    return list(collected.values())


def _valid_request(
    op: PlannedOperation,
    plan: GenerationPlan,
    sampler: Sampler,
    raw_op: dict[str, Any],
    params: list[dict[str, Any]],
) -> _Request:
    path_values: dict[str, str] = {}
    query: dict[str, str] = {}
    headers: dict[str, str] = {}
    for param in params:
        if param["in"] == "cookie":
            if param.get("required"):
                raise SampleError("cookie parameters are not generated")
            continue
        if param["in"] != "path" and not param.get("required"):
            continue
        value = _scalar(sampler.sample(param.get("schema", {"type": "string"}), for_request=True))
        param["value"] = value
        {"path": path_values, "query": query, "header": headers}[param["in"]][param["name"]] = value

    body: Any | None = None
    media: str | None = None
    request_body = raw_op.get("requestBody")
    if isinstance(request_body, dict):
        request_body = sampler.resolve(request_body)
        content = request_body.get("content", {})
        media = next((m for m in content if m in JSON_TYPES or m.endswith("+json")), None)
        if media is None:
            if request_body.get("required"):
                raise SampleError("request body is not JSON")
        else:
            body = sampler.sample(content[media].get("schema"), for_request=True)

    return _Request(
        method=op.method,
        path=_fill_path(plan.base_path, op.path, path_values),
        query=query,
        headers=headers,
        body=body,
        media=media,
        auth=_auth(op),
    )


def _auth(op: PlannedOperation) -> str | None:
    if not op.requires_authentication:
        return None
    first = op.security_requirements[0]
    scopes = sorted({s for values in first.values() for s in values})
    if not scopes:
        return "jwt()"
    grants = ", ".join(f"new SimpleGrantedAuthority({_j('SCOPE_' + s)})" for s in scopes)
    return f"jwt().authorities({grants})"


def _fill_path(base: str, template: str, values: dict[str, str]) -> str:
    path = template
    for name, value in values.items():
        path = path.replace("{" + name + "}", quote(str(value), safe=""))
    return (base.rstrip("/") + "/" + path.lstrip("/")) if base else path


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (dict, list)):
        raise SampleError("object and array parameters are not generated")
    return str(value)


def _replace(request: _Request, **changes: Any) -> _Request:
    return _Request(**{**request.__dict__, **changes})


def _response_content(
    spec: dict[str, Any], sampler: Sampler, raw_op: dict[str, Any], status: int
) -> tuple[str | None, dict[str, Any] | None]:
    responses = raw_op.get("responses", {})
    response = responses.get(str(status)) or responses.get(status)
    if not isinstance(response, dict):
        return None, None
    response = sampler.resolve(response)
    content = response.get("content")
    if not isinstance(content, dict):
        return None, None
    media = next((m for m in content if m in JSON_TYPES or m.endswith("+json")), None)
    if media is None:
        return None, None
    schema = content[media].get("schema")
    return media, schema if isinstance(schema, dict) else None


def _without_required(
    sampler: Sampler, raw_op: dict[str, Any], spec: dict[str, Any], body: Any
) -> tuple[str, Any] | None:
    if not isinstance(body, dict):
        return None
    request_body = sampler.resolve(raw_op["requestBody"])
    media = next(m for m in request_body["content"] if m in JSON_TYPES or m.endswith("+json"))
    schema = sampler.resolve(request_body["content"][media].get("schema", {}))
    required = sorted(r for r in schema.get("required", []) if r in body)
    if not required:
        return None
    name = required[0]
    return name, {k: v for k, v in body.items() if k != name}


def _with_unknown_enum(
    sampler: Sampler, request: _Request, params: list[dict[str, Any]]
) -> _Request | None:
    if request.body is not None and request.media is not None:
        mutated = json.loads(json.dumps(request.body))
        if _walk_enum(mutated, _all_string_enum_values(sampler.spec)):
            return _replace(request, body=mutated)
    for param in params:
        schema = param.get("schema", {})
        if param["in"] == "query" and isinstance(schema.get("enum"), list):
            return _replace(request, query={**request.query, param["name"]: UNKNOWN_ENUM})
    return None


def _walk_enum(body: Any, enums: set[str]) -> bool:
    """Replace the first string enum value in ``body`` with one the spec does not allow."""
    if isinstance(body, dict):
        for key, value in body.items():
            if isinstance(value, str) and value in enums:
                body[key] = UNKNOWN_ENUM
                return True
            if isinstance(value, (dict, list)) and _walk_enum(value, enums):
                return True
    if isinstance(body, list):
        for item in body:
            if _walk_enum(item, enums):
                return True
    return False


def _all_string_enum_values(node: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        enum = node.get("enum")
        if isinstance(enum, list):
            found.update(v for v in enum if isinstance(v, str))
        for value in node.values():
            found |= _all_string_enum_values(value)
    elif isinstance(node, list):
        for item in node:
            found |= _all_string_enum_values(item)
    return found


# ---- Java snippets ----------------------------------------------------------------------


def _perform(request: _Request, expectations: list[str]) -> list[str]:
    builder = [f"request(HttpMethod.{request.method}, {_j(request.path)})"]
    if request.auth:
        builder.append(f".with({request.auth})")
    for name, value in request.query.items():
        builder.append(f".param({_j(name)}, {_j(value)})")
    for name, value in request.headers.items():
        builder.append(f".header({_j(name)}, {_j(value)})")
    if request.body is not None:
        text = request.body if isinstance(request.body, _RAW) else json.dumps(request.body)
        media = request.media or "application/json"
        builder.append(f".contentType(MediaType.parseMediaType({_j(media)}))")
        builder.append(f".content({_j(text)})")
    lines = ["mvc.perform(", "        " + builder[0]]
    lines += ["            " + part for part in builder[1:]]
    lines[-1] += ")"
    lines += [f"    .andExpect({e})" for e in expectations]
    lines[-1] += ";"
    return lines


def _with_distinct_key(lines: list[str], param: dict[str, Any], seed: str) -> list[str]:
    """Swap the sampled Idempotency-Key in ``lines`` for one unique to this test case."""
    current = f".header({_j(param['name'])}, {_j(param['value'])})"
    distinct = _distinct_key(param.get("schema", {}), seed, param["value"])
    replacement = f".header({_j(param['name'])}, {_j(distinct)})"
    return [line.replace(current, replacement) for line in lines]


def _distinct_key(schema: dict[str, Any], seed: str, fallback: str) -> str:
    """A deterministic key for ``seed`` that still satisfies the header's schema."""
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    if schema.get("format") == "uuid":
        return str(uuid.UUID(digest[:32]))
    low = int(schema.get("minLength", 1))
    high = int(schema.get("maxLength", 64))
    candidate = ("o2j" + digest).ljust(low, "0")[: max(low, min(high, 40))]
    pattern = schema.get("pattern")
    if isinstance(pattern, str) and not re.search(pattern, candidate):
        return fallback
    return candidate


def _problem_json() -> str:
    return "content().contentTypeCompatibleWith(MediaType.APPLICATION_PROBLEM_JSON)"


def _stub_respond(operation_id: str, status: int, body: str | None) -> str:
    payload = _j(body) if body is not None else "null"
    return f"stub.respond({_j(operation_id)}, {status}, {payload});"


def _j(text: str | None) -> str:
    """A Java string literal."""
    if text is None:
        return "null"
    # Only when the literal holds something the scaffold renderer would read as a {{token}};
    # Java decodes the escape back to the brace. Otherwise the JSON stays readable.
    escape_braces = TOKEN_LIKE.search(text) is not None
    out = []
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) > 0x7E or (ch in "{}" and escape_braces):
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


# ---- EMS bridge cases -------------------------------------------------------------------


def derive_ems(plan: GenerationPlan, spec: dict[str, Any]) -> dict[str, OperationCases]:
    """Bridge cases per operationId: correlated reply, each mapped fault, and the timeout."""
    sampler = Sampler(spec)
    by_id = {op.operation_id: op for op in plan.operations}
    out: dict[str, OperationCases] = {}
    for route in plan.ems_routes:
        op = by_id[route.operation_id]
        result = out.setdefault(op.operation_id, OperationCases(op.operation_id))
        raw_op = spec["paths"][op.path][op.method.lower()]
        params = _parameters(spec, sampler, op.path, raw_op)
        try:
            request = _valid_request(op, plan, sampler, raw_op, params)
            _, success_schema = _response_content(spec, sampler, raw_op, op.success_status)
            stub_body = (
                json.dumps(sampler.sample(success_schema, for_request=False))
                if success_schema is not None
                else None
            )
        except SampleError as exc:
            result.skipped.append(f"EMS {route.destination}: no valid sample ({exc})")
            continue

        values = {p["name"]: p["value"] for p in params if "value" in p}
        body: Any = request.body
        properties: dict[str, str] = {}
        for param in route.params:
            kind, _, ref = param.source.partition(":")
            if param.name not in values:
                continue
            if kind == "property":
                properties[ref] = values[param.name]
            elif kind == "json":
                body = body if isinstance(body, dict) else {}
                _set_pointer(body, ref, values[param.name])
        message = json.dumps(body) if body is not None else ""
        props = ", ".join(f"Map.entry({_j(k)}, {_j(v)})" for k, v in sorted(properties.items()))
        call = (
            f"Exchange exchange = exchange({_j(route.destination)}, {_j(message)}, "
            f"Map.ofEntries({props}), {_j(EMS_CORRELATION)});"
        )
        expected = (
            "exchange.sentMessageId()" if route.correlation == "message-id" else _j(EMS_CORRELATION)
        )
        oid = op.operation_id
        ident = security.java_identifier(oid)
        dest = security.java_identifier(route.destination)
        reply_ok = (
            "assertThat(exchange.reply()).as(" + _j("reply within the wait") + ").isNotNull();"
        )
        status_is = "assertThat(exchange.reply().getIntProperty(EmsBridge.STATUS)).isEqualTo({});"
        fault_is = "assertThat(exchange.reply().getStringProperty(EmsBridge.FAULT)).isEqualTo({});"

        happy = [
            _stub_respond(oid, op.success_status, stub_body),
            call,
            reply_ok,
            status_is.format(op.success_status),
            f"assertThat(exchange.reply().getJMSCorrelationID()).isEqualTo({expected});",
            "assertThat(exchange.reply().propertyExists(EmsBridge.FAULT)).isFalse();",
        ]
        if stub_body is not None:
            happy.append("assertThat(text(exchange.reply())).isNotBlank();")
        happy.append(f"assertThat(stub.calls({_j(oid)})).isEqualTo(1);")
        result.cases.append(
            ContractCase(
                f"{ident}_via_{dest}_repliesCorrelated", [op.marker("ems:reply-correlated")], happy
            )
        )

        for status, code in route.faults.items():
            result.cases.append(
                ContractCase(
                    f"{ident}_via_{dest}_status{status}_isFault",
                    [op.marker(f"ems:fault-mapped:{status}")],
                    [
                        _stub_respond(oid, status, None),
                        call,
                        reply_ok,
                        status_is.format(status),
                        fault_is.format(_j(code)),
                    ],
                )
            )

        payload = _j(stub_body) if stub_body is not None else "null"
        result.cases.append(
            ContractCase(
                f"{ident}_via_{dest}_slowService_repliesTimeout",
                [op.marker("ems:timeout-504")],
                [
                    f"stub.delay({_j(oid)}, {op.success_status}, {payload}, "
                    f"{route.reply_timeout_ms + 1500}L);",
                    call,
                    reply_ok,
                    status_is.format(504),
                    fault_is.format(_j(route.faults.get(504, "TIMEOUT"))),
                ],
            )
        )
    return out


def _set_pointer(body: dict[str, Any], pointer: str, value: str) -> None:
    parts = [p.replace("~1", "/").replace("~0", "~") for p in pointer.lstrip("/").split("/")]
    node = body
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
