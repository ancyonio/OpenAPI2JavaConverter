"""TIBCO EMS request/reply queues mapped onto spec operations.

The REST service replaces EMS request/response queues, but the consumers of those queues do
not all move on the same day. The generated EMS bridge (``EmsBridge.java``) keeps them working
during the migration: it listens on each legacy request queue, turns the message into the
HTTP call the spec describes, and replies on ``JMSReplyTo`` with the correlation, status and
fault code the consumer already expects. Consumers can then move to HTTP one at a time, and the
bridge is removed when the last one has.

The mapping lives in ``o2j.yaml`` under ``ems.routes`` and is validated here, at plan time,
against the spec - an operation that does not exist, a parameter the operation does not take,
a fault status the operation never returns, or a required parameter with no source is a
configuration error, not a runtime surprise.
"""

from __future__ import annotations

import re
from typing import Any

from ruamel.yaml.comments import CommentedMap

from o2j.config import O2JConfig
from o2j.errors import ConfigError
from o2j.models import PlannedEmsParam, PlannedEmsRoute, PlannedOperation
from o2j.specdoc import resolve_pointer

SOURCE = re.compile(
    r"^(property:[A-Za-z_$][\w$]*|jms:(JMSMessageID|JMSCorrelationID)|json:/\S*|literal:.*)$"
)
REQUIRED_CASES = ("ems:reply-correlated", "ems:timeout-504")


def property_name(param: str) -> str:
    """JMS property names must be Java identifiers: ``Idempotency-Key`` -> ``Idempotency_Key``."""
    cleaned = re.sub(r"[^\w$]", "_", param)
    return cleaned if re.match(r"[A-Za-z_$]", cleaned) else f"_{cleaned}"


def plan_routes(
    cfg: O2JConfig,
    doc: CommentedMap,
    operations: list[PlannedOperation],
    base_path: str,
    parameters: dict[str, list[CommentedMap]],
) -> list[PlannedEmsRoute]:
    """Validate ``ems.routes`` against the spec and return the planned routes.

    ``parameters`` maps operationId to its resolved parameter objects.
    """
    if not cfg.ems.enabled:
        return []
    by_id = {op.operation_id: op for op in operations}
    problems: list[str] = []
    routes: list[PlannedEmsRoute] = []
    seen: set[str] = set()

    if not cfg.ems.routes:
        problems.append("ems.enabled is true but ems.routes is empty")

    for index, route in enumerate(cfg.ems.routes):
        where = f"ems.routes[{index}] ({route.destination})"
        if route.destination in seen:
            problems.append(f"{where}: destination is mapped twice")
        seen.add(route.destination)
        op = by_id.get(route.operation)
        if op is None:
            problems.append(f"{where}: operation {route.operation!r} is not in the spec")
            continue

        timeout = route.reply_timeout_ms or cfg.ems.reply_timeout_ms
        if timeout <= 0:
            problems.append(f"{where}: reply_timeout_ms must be positive")

        declared = {str(p.get("name")): str(p.get("in")) for p in parameters[op.operation_id]}
        for name in route.params:
            if name not in declared:
                problems.append(f"{where}: {op.operation_id} has no parameter {name!r}")
        for name, source in route.params.items():
            if not SOURCE.match(source):
                problems.append(
                    f"{where}: source {source!r} for {name!r} is not property:<id>, "
                    "jms:JMSMessageID, jms:JMSCorrelationID, json:/<pointer> or literal:<v>"
                )

        params: list[PlannedEmsParam] = []
        for param in parameters[op.operation_id]:
            name, location = str(param.get("name")), str(param.get("in"))
            if location == "cookie":
                if param.get("required"):
                    problems.append(f"{where}: cookie parameter {name!r} cannot be bridged")
                continue
            source = route.params.get(name, f"property:{property_name(name)}")
            params.append(PlannedEmsParam(name=name, location=location, source=source))

        statuses = {str(s) for s in _responses(doc, op)}
        for status in route.faults:
            if str(status) not in statuses and f"{str(status)[0]}XX" not in statuses:
                problems.append(
                    f"{where}: fault for {status} maps a status {op.operation_id} never "
                    f"returns (declared: {', '.join(sorted(statuses))})"
                )
            if status < 400:
                problems.append(f"{where}: fault for {status} is not an error status")

        routes.append(
            PlannedEmsRoute(
                destination=route.destination,
                operation_id=op.operation_id,
                method=op.method,
                path=(base_path.rstrip("/") + "/" + op.path.lstrip("/")) if base_path else op.path,
                correlation=route.correlation,
                reply_timeout_ms=timeout,
                has_body=op.request_type is not None,
                params=params,
                faults=dict(sorted(route.faults.items())),
            )
        )

    if problems:
        raise ConfigError(
            "the EMS mapping does not match the spec:\n  " + "\n  ".join(problems),
            hint="fix ems.routes in o2j.yaml; see README, 'Replacing TIBCO EMS queues'",
        )
    return routes


def required_cases(route: PlannedEmsRoute) -> list[str]:
    return [*REQUIRED_CASES, *(f"ems:fault-mapped:{status}" for status in route.faults)]


def _responses(doc: CommentedMap, op: PlannedOperation) -> list[Any]:
    node = resolve_pointer(doc, op.pointer)
    responses = node.get("responses") if isinstance(node, CommentedMap) else None
    return list(responses.keys()) if isinstance(responses, CommentedMap) else []

