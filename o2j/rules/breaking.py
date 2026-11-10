"""FR-V-11: breaking changes between a released spec and its next version.

Both documents are compared in canonical form (OpenAPI 3.1, see stages/validate.py), so a
Swagger 2.0 baseline and a 3.1 successor are compared like for like.

What counts as breaking follows the direction data flows:

- **Requests** (client -> service): anything that makes a request that used to be accepted
  now rejected. A removed request property is breaking here, not merely untidy, because the
  generated service fails on unknown properties (`fail-on-unknown-properties: true`).
- **Responses** (service -> client): anything a client that parsed the old response can no
  longer rely on: a removed or no-longer-required property, a wider type (a value that can
  now be null), a removed success status.

Changes a client may or may not survive - an `operationId` rename, which renames the method
in every generated client, or a new enum value in a response, which breaks clients that map
enums strictly - are reported as warnings.

Composition (`allOf`/`oneOf`/`anyOf`) is not diffed member by member; a change inside a
composed schema is reported only where it surfaces in a plain schema.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from o2j.specdoc import pointer

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
MAX_DEPTH = 12
Direction = Literal["request", "response"]


@dataclass(frozen=True)
class Change:
    rule: str
    pointer: str
    message: str
    breaking: bool  # False: a warning - it may break some clients but not all


def compare(base: dict[str, Any], new: dict[str, Any]) -> list[Change]:
    """Every breaking (or possibly breaking) change from ``base`` to ``new``."""
    return _Diff(base, new).run()


def _template(path: str) -> str:
    """`/orders/{orderId}` and `/orders/{id}` are the same URL to a client."""
    return re.sub(r"\{[^}]*\}", "{}", path)


class _Diff:
    def __init__(self, base: dict[str, Any], new: dict[str, Any]) -> None:
        self.base = base
        self.new = new
        self.changes: list[Change] = []

    # ------------------------------------------------------------------ plumbing

    def _add(self, rule: str, ptr: str, message: str, *, breaking: bool = True) -> None:
        self.changes.append(Change(rule=rule, pointer=ptr, message=message, breaking=breaking))

    @staticmethod
    def _deref(doc: dict[str, Any], node: Any, depth: int = 0) -> Any:
        while isinstance(node, dict) and isinstance(node.get("$ref"), str) and depth < 20:
            ref = node["$ref"]
            if not ref.startswith("#/"):
                return node
            target: Any = doc
            for raw in ref[2:].split("/"):
                token = raw.replace("~1", "/").replace("~0", "~")
                if not isinstance(target, dict) or token not in target:
                    return node
                target = target[token]
            node = target
            depth += 1
        return node

    @staticmethod
    def _operations(doc: dict[str, Any]) -> dict[tuple[str, str], tuple[str, dict[str, Any]]]:
        out: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
        for path, item in (doc.get("paths") or {}).items():
            if not isinstance(item, dict):
                continue
            for method in HTTP_METHODS:
                op = item.get(method)
                if isinstance(op, dict):
                    out[(_template(str(path)), method)] = (str(path), op)
        return out

    # ------------------------------------------------------------------ operations

    def run(self) -> list[Change]:
        base_ops = self._operations(self.base)
        new_ops = self._operations(self.new)

        for key, (path, op) in sorted(base_ops.items()):
            method = key[1]
            where = pointer("paths", path, method)
            label = f"{method.upper()} {path}"
            if key not in new_ops:
                self._add("operation-removed", where, f"{label} was removed.")
                continue
            new_path, new_op = new_ops[key]
            where = pointer("paths", new_path, method)
            self._operation(label, where, path, op, new_path, new_op)

        return self.changes

    def _operation(
        self,
        label: str,
        where: str,
        base_path: str,
        base_op: dict[str, Any],
        new_path: str,
        new_op: dict[str, Any],
    ) -> None:
        old_id, new_id = base_op.get("operationId"), new_op.get("operationId")
        if old_id and new_id and old_id != new_id:
            self._add(
                "operation-id-changed",
                where + "/operationId",
                f"{label}: operationId changed from {old_id} to {new_id}; generated clients "
                "and the delegate method are renamed.",
                breaking=False,
            )

        self._security(label, where, base_op, new_op)
        self._parameters(label, where, base_path, base_op, new_path, new_op)
        self._request_body(label, where, base_op, new_op)
        self._responses(label, where, base_op, new_op)

    def _security(
        self, label: str, where: str, base_op: dict[str, Any], new_op: dict[str, Any]
    ) -> None:
        def effective(doc: dict[str, Any], op: dict[str, Any]) -> list[dict[str, Any]]:
            sec = op.get("security", doc.get("security"))
            return [s for s in (sec or []) if isinstance(s, dict)]

        base_sec, new_sec = effective(self.base, base_op), effective(self.new, new_op)
        base_public = not base_sec or any(not s for s in base_sec)
        new_public = not new_sec or any(not s for s in new_sec)
        if base_public and not new_public:
            self._add(
                "security-added",
                where + "/security",
                f"{label} was public and now requires authentication.",
            )
            return
        if base_public or new_public:
            return

        def scopes(reqs: list[dict[str, Any]]) -> set[str]:
            return {str(s) for req in reqs for values in req.values() for s in (values or [])}

        added = sorted(scopes(new_sec) - scopes(base_sec))
        if added:
            self._add(
                "security-scope-added",
                where + "/security",
                f"{label} now requires scope(s) {', '.join(added)}; existing tokens may lack them.",
            )

    def _params(
        self, doc: dict[str, Any], path: str, op: dict[str, Any]
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """Path-level then operation-level parameters, keyed by (location, name).

        Path parameters are keyed by their position in the template instead: renaming
        `{id}` to `{orderId}` changes nothing a client sends.
        """
        positions = {name: f"#{i}" for i, name in enumerate(re.findall(r"\{([^}]*)\}", path))}
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        item = (doc.get("paths") or {}).get(path) or {}
        for source in (item.get("parameters") or [], op.get("parameters") or []):
            for raw in source:
                param = self._deref(doc, raw)
                if isinstance(param, dict) and "name" in param and "in" in param:
                    location, name = str(param["in"]), str(param["name"])
                    if location == "path":
                        name = positions.get(name, name)
                    merged[(location, name)] = param
        return merged

    def _parameters(
        self,
        label: str,
        where: str,
        base_path: str,
        base_op: dict[str, Any],
        new_path: str,
        new_op: dict[str, Any],
    ) -> None:
        base_params = self._params(self.base, base_path, base_op)
        new_params = self._params(self.new, new_path, new_op)

        for key, param in sorted(new_params.items()):
            location, name = key
            shown = param.get("name", name)
            ptr = f"{where}/parameters"
            if key not in base_params:
                if param.get("required") and location != "path":
                    self._add(
                        "parameter-required-added",
                        ptr,
                        f"{label}: new required {location} parameter `{shown}`; existing "
                        "requests do not send it.",
                    )
                continue
            old = base_params[key]
            if param.get("required") and not old.get("required"):
                self._add(
                    "parameter-became-required",
                    ptr,
                    f"{label}: {location} parameter `{shown}` is now required.",
                )
            self._schema(
                old.get("schema"),
                param.get("schema"),
                f"{ptr}/{shown}/schema",
                f"{label} {location} parameter `{shown}`",
                "request",
            )

    def _request_body(
        self, label: str, where: str, base_op: dict[str, Any], new_op: dict[str, Any]
    ) -> None:
        old = self._deref(self.base, base_op.get("requestBody")) or {}
        new = self._deref(self.new, new_op.get("requestBody")) or {}
        if not isinstance(old, dict) or not isinstance(new, dict):
            return
        ptr = where + "/requestBody"
        if new.get("required") and not old.get("required"):
            self._add(
                "request-body-became-required", ptr, f"{label}: the request body is now required."
            )
        old_content, new_content = old.get("content") or {}, new.get("content") or {}
        for media, media_obj in sorted(old_content.items()):
            if media not in new_content:
                if new_content:
                    self._add(
                        "request-media-type-removed",
                        ptr + "/content",
                        f"{label}: request media type {media} is no longer accepted.",
                    )
                continue
            self._schema(
                (media_obj or {}).get("schema"),
                (new_content[media] or {}).get("schema"),
                pointer(*_tokens(ptr), "content", media, "schema"),
                f"{label} request body",
                "request",
            )

    def _responses(
        self, label: str, where: str, base_op: dict[str, Any], new_op: dict[str, Any]
    ) -> None:
        old_responses = base_op.get("responses") or {}
        new_responses = new_op.get("responses") or {}
        for status, raw in sorted(old_responses.items(), key=lambda kv: str(kv[0])):
            code = str(status)
            if not code.startswith("2"):
                continue
            ptr = pointer(*_tokens(where), "responses", code)
            if code not in {str(s) for s in new_responses}:
                self._add(
                    "response-status-removed",
                    ptr,
                    f"{label}: success response {code} was removed.",
                )
                continue
            old = self._deref(self.base, raw) or {}
            new = self._deref(self.new, new_responses.get(status, new_responses.get(code))) or {}
            old_content = (old.get("content") or {}) if isinstance(old, dict) else {}
            new_content = (new.get("content") or {}) if isinstance(new, dict) else {}
            for media, media_obj in sorted(old_content.items()):
                if media not in new_content:
                    self._add(
                        "response-media-type-removed",
                        ptr + "/content",
                        f"{label}: response {code} no longer returns {media}.",
                    )
                    continue
                self._schema(
                    (media_obj or {}).get("schema"),
                    (new_content[media] or {}).get("schema"),
                    pointer(*_tokens(ptr), "content", media, "schema"),
                    f"{label} response {code}",
                    "response",
                )

    # ------------------------------------------------------------------ schemas

    @staticmethod
    def _types(schema: dict[str, Any]) -> set[str]:
        raw = schema.get("type")
        types = {raw} if isinstance(raw, str) else set(raw) if isinstance(raw, list) else set()
        if schema.get("nullable") is True:
            types.add("null")
        return {str(t) for t in types}

    def _schema(
        self,
        old_raw: Any,
        new_raw: Any,
        ptr: str,
        what: str,
        direction: Direction,
        depth: int = 0,
        seen: frozenset[tuple[str, str]] = frozenset(),
    ) -> None:
        if depth > MAX_DEPTH or old_raw is None or new_raw is None:
            return
        refs = (
            str(old_raw.get("$ref", "")) if isinstance(old_raw, dict) else "",
            str(new_raw.get("$ref", "")) if isinstance(new_raw, dict) else "",
        )
        if refs[0] and refs[1]:
            if refs in seen:
                return  # recursive schema already being compared further up
            seen = seen | {refs}
        old = self._deref(self.base, old_raw)
        new = self._deref(self.new, new_raw)
        if not isinstance(old, dict) or not isinstance(new, dict):
            return

        old_types, new_types = self._types(old), self._types(new)
        if old_types and new_types:
            if direction == "request" and old_types - new_types:
                lost = ", ".join(sorted(old_types - new_types))
                self._add(
                    "request-type-narrowed",
                    ptr,
                    f"{what}: no longer accepts type {lost}.",
                )
            if direction == "response" and new_types - old_types:
                gained = ", ".join(sorted(new_types - old_types))
                self._add(
                    "response-type-widened",
                    ptr,
                    f"{what}: may now return type {gained}, which existing clients do not expect.",
                )
        if old.get("format") and new.get("format") and old["format"] != new["format"]:
            self._add(
                f"{direction}-format-changed",
                ptr,
                f"{what}: format changed from {old['format']} to {new['format']}.",
            )

        old_enum, new_enum = old.get("enum"), new.get("enum")
        if isinstance(old_enum, list) and isinstance(new_enum, list):
            if direction == "request":
                removed = [v for v in old_enum if v not in new_enum]
                if removed:
                    self._add(
                        "request-enum-value-removed",
                        ptr,
                        f"{what}: enum value(s) {_values(removed)} are no longer accepted.",
                    )
            else:
                added = [v for v in new_enum if v not in old_enum]
                if added:
                    self._add(
                        "response-enum-value-added",
                        ptr,
                        f"{what}: may now return enum value(s) {_values(added)}; clients that "
                        "map enums strictly will fail.",
                        breaking=False,
                    )
        elif isinstance(new_enum, list) and old_enum is None and direction == "request":
            self._add(
                "request-enum-added", ptr, f"{what}: is now restricted to {_values(new_enum)}."
            )

        if direction == "request":
            self._constraints(old, new, ptr, what)

        self._properties(old, new, ptr, what, direction, depth, seen)

        if isinstance(old.get("items"), dict) and isinstance(new.get("items"), dict):
            self._schema(
                old["items"], new["items"], ptr + "/items", f"{what}[]", direction, depth + 1, seen
            )

    def _constraints(self, old: dict[str, Any], new: dict[str, Any], ptr: str, what: str) -> None:
        """A tighter limit on input rejects requests that used to be valid."""
        upper = ("maximum", "maxLength", "maxItems", "maxProperties", "exclusiveMaximum")
        lower = ("minimum", "minLength", "minItems", "minProperties", "exclusiveMinimum")
        for key in upper + lower:
            o, n = old.get(key), new.get(key)
            if isinstance(n, bool) or isinstance(o, bool) or not isinstance(n, int | float):
                continue
            tighter = (o is None) or (n < o if key in upper else n > o)
            if tighter:
                before = "unset" if o is None else o
                self._add(
                    "request-constraint-tightened",
                    ptr,
                    f"{what}: {key} tightened from {before} to {n}.",
                )
        if new.get("pattern") and new.get("pattern") != old.get("pattern"):
            self._add(
                "request-pattern-changed",
                ptr,
                f"{what}: pattern changed to {new['pattern']}; previously valid values may fail.",
                breaking=False,
            )

    def _properties(
        self,
        old: dict[str, Any],
        new: dict[str, Any],
        ptr: str,
        what: str,
        direction: Direction,
        depth: int,
        seen: frozenset[tuple[str, str]],
    ) -> None:
        old_props = old.get("properties") or {}
        new_props = new.get("properties") or {}
        if not isinstance(old_props, dict) or not isinstance(new_props, dict):
            return
        old_req = set(old.get("required") or [])
        new_req = set(new.get("required") or [])

        for name in sorted(old_props):
            prop_ptr = pointer(*_tokens(ptr), "properties", name)
            if name not in new_props:
                if direction == "request":
                    message = (
                        f"{what}: property `{name}` was removed; requests that still send it are "
                        "rejected (unknown properties fail)."
                    )
                else:
                    message = f"{what}: property `{name}` was removed from the response."
                self._add(f"{direction}-property-removed", prop_ptr, message)
                continue
            if direction == "response" and name in old_req and name not in new_req:
                self._add(
                    "response-property-became-optional",
                    prop_ptr,
                    f"{what}: property `{name}` is no longer guaranteed to be present.",
                )
            self._schema(
                old_props[name],
                new_props[name],
                prop_ptr,
                f"{what}.{name}",
                direction,
                depth + 1,
                seen,
            )

        if direction == "request":
            for name in sorted(new_req - old_req):
                self._add(
                    "request-property-became-required",
                    pointer(*_tokens(ptr), "properties", name),
                    f"{what}: property `{name}` is now required.",
                )


def _tokens(ptr: str) -> list[str]:
    """Split a JSON Pointer back into its unescaped tokens."""
    if ptr in ("", "#"):
        return []
    return [t.replace("~1", "/").replace("~0", "~") for t in ptr.removeprefix("#/").split("/")]


def _values(values: list[Any]) -> str:
    return ", ".join(repr(v) for v in values[:10]) + (" ..." if len(values) > 10 else "")
