"""FR-V-03: validate a document against the official OpenAPI JSON Schema for its version.

The semantic and house rules look for specific traps; this pass catches everything else the
specification forbids - a misspelt keyword, a `responses` that is a list, a parameter without
`in`. Without it such a document reaches openapi-generator, which either rejects it with a
stack trace or, worse, quietly skips the part it could not read.

The schemas are the ones published by the OpenAPI Initiative, bundled with
`openapi-spec-validator`, so the check runs offline. Only the schema pass is used: the
package's own semantic checks overlap o2j's rules and would report the same problem twice.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from o2j.models import Finding, Location
from o2j.specdoc import line_of, pointer

# More than this many schema errors is one root cause repeated; the rest add noise, not signal.
MAX_FINDINGS = 50


def check_schema(doc: CommentedMap, file: str, family: str) -> list[Finding]:
    """Schema findings for ``doc``, whose version family is ``2.0``, ``3.0``, ``3.1`` or ``3.2``."""
    if family == "3.2":
        return [
            Finding(
                id="O2J-V-003",
                rule="openapi-schema-deferred",
                severity="info",
                message=(
                    "No published JSON Schema validator covers OpenAPI 3.2 yet; the schema "
                    "check runs on the canonical 3.1 document after normalization."
                ),
                pointer="#/openapi",
                location=Location(file=file, line=1, column=1),
                auto_fixable=False,
            )
        ]

    validator = _validator(family)
    if validator is None:
        return [
            Finding(
                id="O2J-V-003",
                rule="openapi-schema-unavailable",
                severity="warn",
                message=(
                    "The OpenAPI JSON Schema check could not run: `openapi-spec-validator` is "
                    "not installed. Structural errors will reach the generator."
                ),
                pointer="#",
                location=Location(file=file),
                remediation="pip install -e . (it is a declared dependency of o2j)",
                auto_fixable=False,
            )
        ]

    # Plain JSON types: YAML timestamps and the like are not JSON, and the schema is.
    plain = json.loads(json.dumps(doc, default=str))
    errors = sorted(validator.iter_errors(plain), key=lambda e: list(map(str, e.absolute_path)))

    findings: list[Finding] = []
    for error in errors:
        detail = _most_specific(error)
        tokens = [str(t) for t in detail.absolute_path]
        line, column = _line_of_path(doc, tokens)
        findings.append(
            Finding(
                id="O2J-V-003",
                rule="openapi-schema-invalid",
                severity="error",
                message=f"Not valid OpenAPI {family}: {_short(detail.message)}",
                pointer=pointer(*tokens),
                location=Location(file=file, line=line, column=column),
                remediation=(
                    "Correct the structure at this location; it does not match the OpenAPI "
                    f"{family} specification."
                ),
                auto_fixable=False,
            )
        )
        if len(findings) == MAX_FINDINGS:
            findings.append(
                Finding(
                    id="O2J-V-003",
                    rule="openapi-schema-invalid",
                    severity="info",
                    message=f"Stopped after {MAX_FINDINGS} schema errors; fix these and re-run.",
                    pointer="#",
                    location=Location(file=file),
                    auto_fixable=False,
                )
            )
            break
    return findings


def _validator(family: str) -> Any | None:
    try:
        from openapi_spec_validator.schemas import (
            openapi_v2_schema_validator,
            openapi_v30_schema_validator,
            openapi_v31_schema_validator,
        )
    except ImportError:
        return None
    return {
        "2.0": openapi_v2_schema_validator,
        "3.0": openapi_v30_schema_validator,
        "3.1": openapi_v31_schema_validator,
    }.get(family)


def _most_specific(error: Any) -> Any:
    """For a oneOf/anyOf failure, the branch error that got furthest into the document.

    The top-level message ("... is not valid under any of the given schemas") says that
    something is wrong without saying what.
    """
    context: Iterable[Any] = getattr(error, "context", None) or []
    candidates = list(context)
    if not candidates:
        return error
    deepest = max(candidates, key=lambda e: (len(e.absolute_path), -len(e.message)))
    if len(deepest.absolute_path) >= len(error.absolute_path):
        return _most_specific(deepest)
    return error


def _short(message: str, limit: int = 300) -> str:
    return message if len(message) <= limit else message[: limit - 3] + "..."


def _line_of_path(doc: Any, tokens: list[str]) -> tuple[int | None, int | None]:
    """The source line of the deepest node on ``tokens`` that exists in ``doc``."""
    node: Any = doc
    best: tuple[int | None, int | None] = (1, 1)
    for token in tokens:
        if isinstance(node, CommentedMap) and token in node:
            found = line_of(node, token)
            node = node[token]
        elif isinstance(node, CommentedSeq) and token.isdigit() and int(token) < len(node):
            found = line_of(node, int(token))
            node = node[int(token)]
        else:
            break
        if found[0] is not None:
            best = found
    return best
