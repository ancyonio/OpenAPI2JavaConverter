"""Synthesize schema-valid sample values from the canonical spec.

Generated contract tests need a valid request (and a valid stub response) for every operation.
Values come from, in order: ``const``, ``example``/``examples``, ``default``, ``enum``, then a
synthesized value honouring type, format, bounds, length and simple patterns.

Every sample is validated against its own schema before it is used. A sample that does not
validate is discarded and the cases that needed it are left for a person to write - a test
built on a plausible-but-wrong sample would fail for the wrong reason, or worse, pass.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

SPEC_URI = "urn:o2j:spec"
MAX_DEPTH = 8

FORMAT_SAMPLES: dict[str, Any] = {
    "uuid": "3f2504e0-4f89-41d3-9a0c-0305e82c3301",
    "date-time": "2026-01-15T10:15:30Z",
    "date": "2026-01-15",
    "time": "10:15:30Z",
    "email": "o2j@example.com",
    "uri": "https://example.com/o2j",
    "url": "https://example.com/o2j",
    "hostname": "example.com",
    "ipv4": "192.0.2.1",
    "ipv6": "2001:db8::1",
    "byte": "bzJq",
    "password": "o2j-sample-secret",
}


class SampleError(Exception):
    """A schema could not be satisfied by synthesis."""


class Sampler:
    """Builds values for schemas inside one OpenAPI document."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec
        resource = Resource.from_contents(spec, default_specification=DRAFT202012)
        self.registry: Registry[Any] = Registry().with_resource(SPEC_URI, resource)

    # ---- public -------------------------------------------------------------------------

    def sample(self, schema: dict[str, Any] | None, *, for_request: bool) -> Any:
        """A value valid against ``schema``; raises SampleError when none can be found."""
        if schema is None:
            raise SampleError("no schema")
        value = self._sample(schema, for_request, 0)
        if not self.is_valid(value, schema):
            raise SampleError(f"synthesized value does not validate: {value!r}")
        return value

    def is_valid(self, value: Any, schema: dict[str, Any]) -> bool:
        return not list(self.validator(schema).iter_errors(value))

    def validator(self, schema: dict[str, Any]) -> Draft202012Validator:
        return Draft202012Validator(
            self._anchored(schema), registry=self.registry, format_checker=FormatChecker()
        )

    def resolve(self, schema: Any) -> dict[str, Any]:
        seen = 0
        while isinstance(schema, dict) and isinstance(schema.get("$ref"), str) and seen < 32:
            schema = self.pointer(schema["$ref"])
            seen += 1
        if not isinstance(schema, dict):
            raise SampleError("unresolvable $ref")
        return schema

    def pointer(self, ref: str) -> Any:
        if not ref.startswith("#/"):
            raise SampleError(f"only local $refs are supported: {ref}")
        node: Any = self.spec
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                raise SampleError(f"dangling $ref {ref}")
        return node

    # ---- synthesis ----------------------------------------------------------------------

    def _anchored(self, schema: dict[str, Any]) -> dict[str, Any]:
        """Point local $refs at the registered spec so validation can follow them."""
        text = json.dumps(schema)
        return dict(json.loads(text.replace('"#/', f'"{SPEC_URI}#/')))

    def _sample(self, schema: Any, for_request: bool, depth: int) -> Any:
        if depth > MAX_DEPTH:
            raise SampleError("schema nests too deeply to sample")
        schema = self.resolve(schema)

        for key in ("const",):
            if key in schema:
                return copy.deepcopy(schema[key])
        if "example" in schema:
            return copy.deepcopy(schema["example"])
        examples = schema.get("examples")
        if isinstance(examples, list) and examples:
            return copy.deepcopy(examples[0])
        if "default" in schema:
            return copy.deepcopy(schema["default"])
        enum = schema.get("enum")
        if isinstance(enum, list):
            non_null = [v for v in enum if v is not None]
            if non_null:
                return copy.deepcopy(non_null[0])

        if isinstance(schema.get("allOf"), list):
            merged: dict[str, Any] = {}
            for part in schema["allOf"]:
                merged = _merge(merged, self.resolve(part))
            rest = {k: v for k, v in schema.items() if k != "allOf"}
            return self._sample(_merge(merged, rest), for_request, depth + 1)
        for key in ("oneOf", "anyOf"):
            options = schema.get(key)
            if isinstance(options, list) and options:
                return self._choose(schema, options, for_request, depth)

        kind = _type_of(schema)
        if kind == "object":
            return self._object(schema, for_request, depth)
        if kind == "array":
            return self._array(schema, for_request, depth)
        if kind == "string":
            return _string(schema)
        if kind == "integer":
            return int(_number(schema, integer=True))
        if kind == "number":
            return _number(schema, integer=False)
        if kind == "boolean":
            return True
        if kind == "null":
            return None
        raise SampleError(f"cannot sample schema without a usable type: {schema}")

    def _choose(
        self, schema: dict[str, Any], options: list[Any], for_request: bool, depth: int
    ) -> Any:
        discriminator = schema.get("discriminator")
        for option in options:
            try:
                value = self._sample(option, for_request, depth + 1)
            except SampleError:
                continue
            if isinstance(discriminator, dict) and isinstance(value, dict):
                prop = discriminator.get("propertyName")
                if prop and isinstance(option, dict) and "$ref" in option:
                    value[prop] = _discriminator_value(discriminator, option["$ref"])
            return value
        raise SampleError("no oneOf/anyOf branch could be sampled")

    def _object(self, schema: dict[str, Any], for_request: bool, depth: int) -> dict[str, Any]:
        out: dict[str, Any] = {}
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        for name, prop in props.items():
            resolved = self.resolve(prop)
            if for_request and resolved.get("readOnly"):
                continue
            if not for_request and resolved.get("writeOnly"):
                continue
            out[name] = self._sample(prop, for_request, depth + 1)
        return out

    def _array(self, schema: dict[str, Any], for_request: bool, depth: int) -> list[Any]:
        items = schema.get("items", {})
        count = max(1, int(schema.get("minItems", 1)))
        first = self._sample(items, for_request, depth + 1)
        if count == 1:
            return [first]
        if schema.get("uniqueItems"):
            raise SampleError("cannot synthesize several unique items")
        return [copy.deepcopy(first) for _ in range(count)]


def _merge(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    out = dict(left)
    for key, value in right.items():
        if key == "properties" and isinstance(value, dict):
            out["properties"] = {**out.get("properties", {}), **value}
        elif key == "required" and isinstance(value, list):
            out["required"] = sorted({*out.get("required", []), *value})
        else:
            out[key] = value
    return out


def _type_of(schema: dict[str, Any]) -> str | None:
    kind = schema.get("type")
    if isinstance(kind, list):
        non_null = [k for k in kind if k != "null"]
        return str(non_null[0]) if non_null else "null"
    if isinstance(kind, str):
        return kind
    if "properties" in schema:
        return "object"
    if "items" in schema:
        return "array"
    return None


def _discriminator_value(discriminator: dict[str, Any], ref: str) -> str:
    mapping = discriminator.get("mapping")
    if isinstance(mapping, dict):
        for key, target in mapping.items():
            if target == ref or str(target).endswith("/" + ref.rsplit("/", 1)[-1]):
                return str(key)
    return ref.rsplit("/", 1)[-1]


def _number(schema: dict[str, Any], *, integer: bool) -> float:
    step = 1.0
    low = schema.get("minimum")
    excl_low = schema.get("exclusiveMinimum")
    high = schema.get("maximum")
    excl_high = schema.get("exclusiveMaximum")

    floor: float | None = None
    if isinstance(low, (int, float)) and not isinstance(low, bool):
        floor = float(low)
    if isinstance(excl_low, (int, float)) and not isinstance(excl_low, bool):
        floor = max(floor if floor is not None else excl_low, float(excl_low) + step)
    elif excl_low is True and floor is not None:  # 3.0 boolean form, if it survived
        floor += step

    ceiling: float | None = None
    if isinstance(high, (int, float)) and not isinstance(high, bool):
        ceiling = float(high)
    if isinstance(excl_high, (int, float)) and not isinstance(excl_high, bool):
        ceiling = min(ceiling if ceiling is not None else excl_high, float(excl_high) - step)

    value = floor if floor is not None else (min(1.0, ceiling) if ceiling is not None else 1.0)
    if ceiling is not None and value > ceiling:
        raise SampleError("numeric bounds leave no room for a value")
    multiple = schema.get("multipleOf")
    if isinstance(multiple, (int, float)) and multiple > 0:
        value = float(multiple) * max(1, int(value // multiple) + (1 if value % multiple else 0))
    return float(int(value)) if integer else value


def _string(schema: dict[str, Any]) -> str:
    fmt = schema.get("format")
    if isinstance(fmt, str) and fmt in FORMAT_SAMPLES:
        return str(FORMAT_SAMPLES[fmt])
    pattern = schema.get("pattern")
    if isinstance(pattern, str):
        value = from_pattern(pattern)
    else:
        value = "o2j-sample"
    low = int(schema.get("minLength", 0))
    high = schema.get("maxLength")
    if len(value) < low:
        if isinstance(pattern, str):
            raise SampleError("pattern sample is shorter than minLength")
        value = value + "x" * (low - len(value))
    if isinstance(high, int) and len(value) > high:
        if isinstance(pattern, str):
            raise SampleError("pattern sample is longer than maxLength")
        value = value[:high]
    return value


# A deliberately small regex sampler: literals, escapes, classes, groups of literals and the
# quantifiers ?, *, +, {n}, {n,m}. Anything else raises, and the sample is validated anyway.
_TOKEN = re.compile(
    r"\\d|\\w|\\s|\\.|\\[^dws]|\[[^\]]+\]|\((?:\?:)?[^()|]*\)|[^\\\[\]()|*+?{}^$]"
)


def from_pattern(pattern: str) -> str:
    body = pattern
    if body.startswith("^"):
        body = body[1:]
    if body.endswith("$") and not body.endswith("\\$"):
        body = body[:-1]
    out: list[str] = []
    i = 0
    while i < len(body):
        match = _TOKEN.match(body, i)
        if not match:
            raise SampleError(f"unsupported pattern construct at {body[i:]!r}")
        token = match.group(0)
        i = match.end()
        count = 1
        quant = re.match(r"\{(\d+)(?:,(\d*))?\}|[?*+]", body[i:])
        if quant:
            text = quant.group(0)
            i += len(text)
            if text == "?" or text == "*":
                count = 0 if text == "*" else 1
            elif text == "+":
                count = 1
            else:
                count = int(quant.group(1))
        out.append(_token_char(token) * count)
    value = "".join(out)
    if not re.fullmatch(pattern.lstrip("^").rstrip("$") if pattern else "", value):
        raise SampleError(f"pattern sampler produced {value!r} for {pattern!r}")
    return value


def _token_char(token: str) -> str:
    if token == r"\d":
        return "1"
    if token == r"\w":
        return "a"
    if token == r"\s":
        return " "
    if token.startswith("\\"):
        return token[1]
    if token.startswith("["):
        inner = token[1:-1]
        if inner.startswith("^"):
            raise SampleError("negated character classes are not sampled")
        first = inner[0] if inner[0] != "\\" else inner[1]
        return {"d": "1", "w": "a"}.get(first, first) if inner[0] == "\\" else first
    if token.startswith("("):
        inner = token[1:-1]
        return inner[2:] if inner.startswith("?:") else inner
    return token
