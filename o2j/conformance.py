"""Check the HTTP exchanges recorded during the test run against the spec.

``O2jConformanceRecorder`` (a test-scope servlet filter in every generated service) writes one
JSON line per exchange to ``target/o2j-conformance/exchanges.jsonl``. For each exchange that
belongs to a spec operation this module checks:

* the status is declared for the operation (exact, ``4XX``-style range, or ``default``);
* a non-empty body has a content type the response declares;
* the body is valid against that media type's schema (JSON Schema 2020-12, which is the
  dialect of the canonical 3.1 document, with formats checked).

This is what turns "the tests pass" into "what the service actually returned matches the
contract". Line coverage cannot see a renamed JSON property or an undeclared 500; this can.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from o2j.samples import Sampler

EXCHANGES = Path("target") / "o2j-conformance" / "exchanges.jsonl"


@dataclass
class OperationConformance:
    exchanges: int = 0
    conforming_success: int = 0  # 2xx exchanges that passed every check
    violations: list[str] = field(default_factory=list)


@dataclass
class ConformanceResult:
    found: bool
    exchanges: int = 0
    checked: int = 0
    ignored: int = 0  # exchanges outside the spec (actuator, refused unknown routes)
    by_operation: dict[str, OperationConformance] = field(default_factory=dict)

    @property
    def violations(self) -> list[str]:
        return [v for op in self.by_operation.values() for v in op.violations]


@dataclass(frozen=True)
class _Route:
    operation_id: str
    method: str
    path: str
    regex: re.Pattern[str]


def check(project: Path, spec: dict[str, Any], base_path: str) -> ConformanceResult:
    path = project / EXCHANGES
    if not path.is_file():
        return ConformanceResult(found=False)

    sampler = Sampler(spec)
    routes = _routes(spec, base_path)
    result = ConformanceResult(found=True)

    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        result.exchanges += 1
        try:
            exchange = json.loads(line)
        except json.JSONDecodeError:
            result.ignored += 1
            continue
        route = _match(routes, str(exchange.get("method", "")), str(exchange.get("path", "")))
        if route is None:
            result.ignored += 1
            continue
        result.checked += 1
        stats = result.by_operation.setdefault(route.operation_id, OperationConformance())
        stats.exchanges += 1
        problems = _check_exchange(sampler, spec, route, exchange)
        where = f"{route.operation_id} {exchange.get('method')} {exchange.get('path')}"
        status = exchange.get("status")
        stats.violations += [f"{where} -> {status}: {p} (exchange {number})" for p in problems]
        if not problems and isinstance(status, int) and 200 <= status < 300:
            stats.conforming_success += 1
    return result


def _routes(spec: dict[str, Any], base_path: str) -> list[_Route]:
    out: list[_Route] = []
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        full = (base_path.rstrip("/") + "/" + path.lstrip("/")) if base_path else path
        pattern = "^" + re.sub(r"\\\{[^/]+?\\\}", "[^/]+", re.escape(full)) + "/?$"
        for method, op in item.items():
            if isinstance(op, dict) and "responses" in op:
                out.append(
                    _Route(
                        operation_id=str(op.get("operationId") or f"{method} {path}"),
                        method=method.upper(),
                        path=path,
                        regex=re.compile(pattern),
                    )
                )
    # Literal segments before templated ones, as Spring MVC matches.
    return sorted(out, key=lambda r: (r.path.count("{"), r.path))


def _match(routes: list[_Route], method: str, path: str) -> _Route | None:
    for route in routes:
        if route.method == method.upper() and route.regex.match(path):
            return route
    return None


def _check_exchange(
    sampler: Sampler, spec: dict[str, Any], route: _Route, exchange: dict[str, Any]
) -> list[str]:
    status = exchange.get("status")
    if not isinstance(status, int):
        return ["recorded exchange has no status"]
    if status == 501:
        return ["501 Not Implemented: the operation is still a stub"]

    operation = spec["paths"][route.path][route.method.lower()]
    responses = operation.get("responses", {})
    response = (
        responses.get(str(status))
        or responses.get(f"{str(status)[0]}XX")
        or responses.get("default")
    )
    if response is None:
        declared = ", ".join(sorted(str(k) for k in responses))
        return [f"status {status} is not declared (declared: {declared})"]
    response = sampler.resolve(response)

    body = exchange.get("body") or ""
    if not body.strip():
        return []

    content = response.get("content") or {}
    if not content:
        return ["a body was returned but the response declares no content"]
    media = _media_type(exchange.get("contentType"))
    if media not in content:
        return [f"content type {media or 'none'} is not declared (declared: {', '.join(content)})"]
    schema = content[media].get("schema")
    if not isinstance(schema, dict) or not media or not media.endswith("json"):
        return []
    try:
        value = json.loads(body)
    except json.JSONDecodeError:
        return [f"body is not valid JSON although {media} was declared"]

    errors = sorted(sampler.validator(schema).iter_errors(value), key=lambda e: list(e.path))
    return [
        f"body does not match the schema at /{'/'.join(str(p) for p in e.path)}: {e.message}"
        for e in errors[:5]
    ]


def _media_type(content_type: Any) -> str | None:
    if not isinstance(content_type, str) or not content_type:
        return None
    return content_type.split(";", 1)[0].strip().lower()
