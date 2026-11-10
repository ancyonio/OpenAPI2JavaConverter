"""Loading, pointing into, and hashing an OpenAPI document.

The spec is untrusted input (SPECIFICATION.md Section 5.3), so loading goes through
ruamel's round-trip loader rather than anything that can construct Python objects. Round-trip
mode is chosen over safe mode for one reason: it preserves line and column numbers, which is
what lets a finding say *where* rather than only *what*.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.constructor import DuplicateKeyError

from o2j.errors import IngestError

HTTP_METHODS: tuple[str, ...] = (
    "get",
    "put",
    "post",
    "delete",
    "options",
    "head",
    "patch",
    "trace",
)


def escape_token(token: str) -> str:
    """Escape one JSON Pointer token per RFC 6901."""
    return token.replace("~", "~0").replace("/", "~1")


def pointer(*tokens: str | int) -> str:
    """Build a JSON Pointer from path tokens."""
    if not tokens:
        return "#"
    return "#/" + "/".join(escape_token(str(t)) for t in tokens)


def rt_yaml() -> YAML:
    """Round-trip YAML configured for deterministic output."""
    y = YAML()
    y.preserve_quotes = True
    y.default_flow_style = False
    y.width = 4096  # never wrap; wrapping is a diff-noise source
    y.indent(mapping=2, sequence=4, offset=2)
    y.allow_duplicate_keys = False
    return y


def load_spec(path: Path) -> CommentedMap:
    """Parse an OpenAPI document, rejecting duplicate keys.

    A duplicate key is silently last-wins in most YAML tooling, which means a spec can
    declare one thing and generate another. It is treated as a hard parse failure here.
    """
    text = path.read_text(encoding="utf-8")
    try:
        doc: Any = rt_yaml().load(text)
    except DuplicateKeyError as exc:
        raise IngestError(
            f"{path.name} contains a duplicate key: {exc}",
            hint="duplicate keys are silently last-wins in most tooling; remove the duplicate",
        ) from exc
    except Exception as exc:
        raise IngestError(f"{path.name} is not valid YAML or JSON: {exc}") from exc

    if not isinstance(doc, CommentedMap):
        raise IngestError(f"{path.name} must contain a mapping at the top level")
    return doc


def dump_spec(doc: Any) -> str:
    """Serialise a document to a deterministic string.

    Byte stability matters: the canonical spec's hash is the identity of the generated code,
    so an unchanged input must always produce an identical string (FR-N-07).
    """
    buf = io.StringIO()
    rt_yaml().dump(doc, buf)
    # Normalise line endings so a Windows checkout and a Linux CI run agree.
    return buf.getvalue().replace("\r\n", "\n")


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def line_of(node: Any, key: str | int | None = None) -> tuple[int | None, int | None]:
    """Best-effort line and column for a node, or for one key within it.

    ruamel attaches position data only to round-trip containers, so this returns ``(None,
    None)`` for plain values rather than guessing.
    """
    lc = getattr(node, "lc", None)
    if lc is None:
        return None, None
    if key is not None:
        try:
            if isinstance(node, CommentedMap):
                pos = lc.key(key)
            elif isinstance(node, CommentedSeq) and isinstance(key, int):
                pos = lc.item(key)
            else:
                pos = (lc.line, lc.col)
        except (KeyError, IndexError, AttributeError):
            pos = (lc.line, lc.col)
    else:
        pos = (lc.line, lc.col)
    if pos is None:
        return None, None
    return int(pos[0]) + 1, int(pos[1]) + 1


def detect_version(doc: CommentedMap) -> str:
    """Return the declared spec version, or ``unknown``."""
    if "openapi" in doc:
        return str(doc["openapi"]).strip()
    if "swagger" in doc:
        return str(doc["swagger"]).strip()
    return "unknown"


def version_family(version: str) -> str:
    """Collapse a version to the family whose rules apply: ``2.0``, ``3.0``, ``3.1``, ``3.2``."""
    if version.startswith("2."):
        return "2.0"
    if version.startswith("3.0"):
        return "3.0"
    if version.startswith("3.1"):
        return "3.1"
    if version.startswith("3.2"):
        return "3.2"
    return "unknown"


def iter_operations(doc: CommentedMap) -> list[tuple[str, str, CommentedMap]]:
    """Yield ``(path, method, operation)`` for every operation in the document."""
    out: list[tuple[str, str, CommentedMap]] = []
    paths = doc.get("paths")
    if not isinstance(paths, CommentedMap):
        return out
    for path, item in paths.items():
        if not isinstance(item, CommentedMap):
            continue
        for method in HTTP_METHODS:
            op = item.get(method)
            if isinstance(op, CommentedMap):
                out.append((str(path), method, op))
    return out


def iter_schemas(doc: CommentedMap) -> list[tuple[str, CommentedMap]]:
    """Yield ``(pointer, schema)`` for every schema-like mapping in the document.

    Walks the whole tree rather than only ``components.schemas``, because the traps worth
    catching (3.0 ``nullable`` in a 3.1 document, boolean ``exclusiveMinimum``) appear just
    as often in inline parameter and response schemas.
    """
    found: list[tuple[str, CommentedMap]] = []
    seen: set[int] = set()

    def walk(node: Any, trail: list[str | int]) -> None:
        if id(node) in seen:
            return
        if isinstance(node, CommentedMap):
            seen.add(id(node))
            if _looks_like_schema(node):
                found.append((pointer(*trail), node))
            for k, v in node.items():
                walk(v, [*trail, str(k)])
        elif isinstance(node, CommentedSeq):
            seen.add(id(node))
            for i, v in enumerate(node):
                walk(v, [*trail, i])

    walk(doc, [])
    return found


_SCHEMA_KEYS = frozenset(
    {
        "type",
        "properties",
        "items",
        "allOf",
        "oneOf",
        "anyOf",
        "enum",
        "format",
        "nullable",
        "additionalProperties",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minimum",
        "maximum",
        "required",
    }
)


def _looks_like_schema(node: CommentedMap) -> bool:
    return bool(_SCHEMA_KEYS & set(node.keys()))


def resolve_pointer(doc: CommentedMap, ref: str) -> Any | None:
    """Resolve a local ``#/...`` JSON Pointer, or return ``None`` if it does not resolve."""
    if not ref.startswith("#/"):
        return None
    node: Any = doc
    for raw in ref[2:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, CommentedMap):
            if token not in node:
                return None
            node = node[token]
        elif isinstance(node, CommentedSeq):
            try:
                node = node[int(token)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return node
