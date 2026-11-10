"""House REST and API standards (FR-V-10).

These are conventions rather than correctness bugs, but they are enforced at generation time
because they are nearly impossible to retrofit: a path or an error envelope becomes part of
the published contract the moment a client consumes it.

The equivalent Spectral ruleset in ``o2j/rules/spectral/house-api.yaml`` mirrors these checks
for teams that lint specs in their editor; this module is the one the pipeline gates on, so
it needs no Node toolchain to run.
"""

from __future__ import annotations

import re

from ruamel.yaml.comments import CommentedMap

from o2j.config import O2JConfig
from o2j.models import Finding, Location
from o2j.specdoc import iter_operations, line_of, pointer

KEBAB_SEGMENT = re.compile(r"^([a-z0-9]+(-[a-z0-9]+)*|\{[a-zA-Z0-9_]+\})$")
PATH_VERBS = re.compile(
    r"^(get|create|update|delete|fetch|list|put|post|add|remove|retrieve|search)$", re.I
)
PAGINATION_PARAMS = frozenset({"page", "size", "limit", "cursor", "offset", "pageToken"})
PROBLEM_JSON = "application/problem+json"

# Error statuses every operation must declare, and the extra pair required when the
# operation is authenticated.
REQUIRED_ERROR_STATUSES: tuple[str, ...] = ("400", "500")
AUTHENTICATED_ERROR_STATUSES: tuple[str, ...] = ("401", "403")


def _loc(file: str, node: object, key: str | int | None = None) -> Location:
    line, col = line_of(node, key)
    return Location(file=file, line=line, column=col)


def check_paths(doc: CommentedMap, file: str) -> list[Finding]:
    """Paths must be kebab-case, noun-based collections. Actions are HTTP methods."""
    findings: list[Finding] = []
    paths = doc.get("paths")
    if not isinstance(paths, CommentedMap):
        return findings

    for raw_path in paths:
        path = str(raw_path)
        segments = [s for s in path.split("/") if s]

        for segment in segments:
            if not KEBAB_SEGMENT.match(segment):
                findings.append(
                    Finding(
                        id="O2J-V-010",
                        rule="path-kebab-case",
                        severity="error",
                        message=(
                            f"Path segment {segment!r} in {path} is not kebab-case. Paths are "
                            f"URLs, not Java identifiers."
                        ),
                        pointer=pointer("paths", path),
                        location=_loc(file, paths, raw_path),
                        remediation=f"Rename the segment to `{_to_kebab(segment)}`.",
                        auto_fixable=True,
                    )
                )
            if PATH_VERBS.match(segment):
                findings.append(
                    Finding(
                        id="O2J-V-010",
                        rule="no-verbs-in-path",
                        severity="error",
                        message=(
                            f"Path segment {segment!r} in {path} is a verb. Express the action "
                            f"with the HTTP method and keep the path a noun."
                        ),
                        pointer=pointer("paths", path),
                        location=_loc(file, paths, raw_path),
                        remediation="Remove the verb segment and use the matching HTTP method.",
                        auto_fixable=False,
                    )
                )

    return findings


def _to_kebab(value: str) -> str:
    if value.startswith("{") and value.endswith("}"):
        return value
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", value)
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s)
    return s.strip("-").lower()


def check_error_responses(doc: CommentedMap, file: str, cfg: O2JConfig) -> list[Finding]:
    """Every operation declares a complete, consistent error contract.

    An undeclared error status is one a client cannot handle and the generator cannot type,
    so the error path ends up untested and the response shape ends up inconsistent.
    """
    findings: list[Finding] = []
    has_global_security = bool(doc.get("security"))

    for path, method, op in iter_operations(doc):
        ptr = pointer("paths", path, method, "responses")
        responses = op.get("responses")
        if not isinstance(responses, CommentedMap):
            findings.append(
                Finding(
                    id="O2J-V-010",
                    rule="responses-required",
                    severity="error",
                    message=f"{method.upper()} {path} declares no responses.",
                    pointer=pointer("paths", path, method),
                    location=_loc(file, op),
                    remediation="Declare at least one success response and 400/500.",
                    auto_fixable=False,
                )
            )
            continue

        declared = {str(k) for k in responses}
        authenticated = bool(op.get("security")) or (
            has_global_security and op.get("security") is None
        )
        required = list(REQUIRED_ERROR_STATUSES)
        if authenticated:
            required += list(AUTHENTICATED_ERROR_STATUSES)

        missing = [s for s in required if s not in declared]
        if missing:
            findings.append(
                Finding(
                    id="O2J-V-010",
                    rule="error-responses-declared",
                    severity="error",
                    message=(
                        f"{method.upper()} {path} does not declare {', '.join(missing)}. "
                        f"Undeclared error responses are untyped for clients and untested here."
                    ),
                    pointer=ptr,
                    location=_loc(file, op, "responses"),
                    remediation=(
                        f"Add {', '.join(missing)} with `content: "
                        f"{{{PROBLEM_JSON}: {{schema: ...}}}}`."
                    ),
                    auto_fixable=False,
                )
            )

        if cfg.validation.require_problem_details:
            findings.extend(_check_problem_details(responses, path, method, file))

    return findings


def _check_problem_details(
    responses: CommentedMap, path: str, method: str, file: str
) -> list[Finding]:
    findings: list[Finding] = []
    for status, response in responses.items():
        status_str = str(status)
        if status_str[:1] not in ("4", "5"):
            continue
        if not isinstance(response, CommentedMap) or "$ref" in response:
            continue
        content = response.get("content")
        if not isinstance(content, CommentedMap):
            continue
        if PROBLEM_JSON not in content:
            findings.append(
                Finding(
                    id="O2J-V-010",
                    rule="problem-details-media-type",
                    severity="error",
                    message=(
                        f"{method.upper()} {path} response {status_str} does not use "
                        f"{PROBLEM_JSON}. Error shapes must be consistent across the API "
                        f"(RFC 9457)."
                    ),
                    pointer=pointer(
                        "paths", path, method, "responses", status_str, "content"
                    ),
                    location=_loc(file, response),
                    remediation=f"Serve the error body as {PROBLEM_JSON}.",
                    auto_fixable=True,
                )
            )
    return findings


def check_pagination(doc: CommentedMap, file: str, cfg: O2JConfig) -> list[Finding]:
    """Collection GETs must be paginated.

    An unpaginated collection works in development and falls over in production once the
    table grows. Reported as a warning because a genuinely bounded collection is a valid
    exception, and the author is better placed to know.
    """
    if not cfg.validation.require_pagination_on_collections:
        return []

    findings: list[Finding] = []
    paths = doc.get("paths")
    if not isinstance(paths, CommentedMap):
        return findings

    for raw_path, item in paths.items():
        path = str(raw_path)
        if not isinstance(item, CommentedMap):
            continue
        op = item.get("get")
        if not isinstance(op, CommentedMap):
            continue
        # A collection path ends in a plain segment, not a path parameter.
        segments = [s for s in path.split("/") if s]
        if not segments or segments[-1].startswith("{"):
            continue
        if not _returns_array(op):
            continue

        params = list(op.get("parameters") or []) + list(item.get("parameters") or [])
        names = {
            str(p.get("name"))
            for p in params
            if isinstance(p, CommentedMap) and p.get("name")
        }
        if not (names & PAGINATION_PARAMS):
            findings.append(
                Finding(
                    id="O2J-V-010",
                    rule="collection-pagination",
                    severity="warn",
                    message=(
                        f"GET {path} returns a collection with no pagination parameters. "
                        f"Unbounded collections fail once the data grows."
                    ),
                    pointer=pointer("paths", path, "get", "parameters"),
                    location=_loc(file, op),
                    remediation=(
                        "Add `page` and `size`, or a `cursor` parameter, and return a page "
                        "envelope."
                    ),
                    auto_fixable=False,
                )
            )

    return findings


def _returns_array(op: CommentedMap) -> bool:
    responses = op.get("responses")
    if not isinstance(responses, CommentedMap):
        return False
    for status, response in responses.items():
        if not str(status).startswith("2") or not isinstance(response, CommentedMap):
            continue
        content = response.get("content")
        if not isinstance(content, CommentedMap):
            continue
        for media in content.values():
            if not isinstance(media, CommentedMap):
                continue
            schema = media.get("schema")
            if isinstance(schema, CommentedMap) and (
                schema.get("type") == "array" or "items" in schema
            ):
                return True
    return False


def run_all(doc: CommentedMap, file: str, cfg: O2JConfig) -> list[Finding]:
    """Run every house-standards check and return the combined findings."""
    return [
        *check_paths(doc, file),
        *check_error_responses(doc, file, cfg),
        *check_pagination(doc, file, cfg),
    ]
