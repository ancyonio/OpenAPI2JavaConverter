"""A deliberately small Mustache subset for rendering scaffold files.

Only ``{{var}}``, ``{{{var}}}`` (unescaped), ``{{#section}}``/``{{/section}}`` over lists or
truthy values, and ``{{^section}}`` for the empty case. That is everything the scaffold
templates need, and keeping it here avoids a dependency whose version would then need pinning
and auditing like every other.

The full Mustache implementation that openapi-generator uses for the contract layer is
openapi-generator's own: ``o2j/templates`` overrides are passed through to it untouched.
"""

from __future__ import annotations

import re
from typing import Any

SECTION = re.compile(r"\{\{([#^])\s*([\w.]+)\s*\}\}(.*?)\{\{/\s*\2\s*\}\}", re.DOTALL)

# Two explicit alternatives, triple first, rather than one pattern with optional braces.
# An optional-brace pattern reads `{{var}}}` as the triple form and swallows the third `}`,
# which silently ate the closing brace of a Javadoc `{@code ...}` in a scaffold template.
VARIABLE = re.compile(r"\{\{\{\s*([\w.]+)\s*\}\}\}|\{\{\s*([\w.]+)\s*\}\}")


def render(template: str, context: dict[str, Any]) -> str:
    """Render ``template`` against ``context``."""
    out = _render_sections(template, context)
    return _render_variables(out, context)


def _render_sections(template: str, context: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        kind, name, body = match.group(1), match.group(2), match.group(3)
        value = _lookup(context, name)

        if kind == "^":
            return _render_sections(body, context) if not _truthy(value) else ""

        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                scoped = {**context, **item} if isinstance(item, dict) else {**context, ".": item}
                parts.append(render(body, scoped))
            return "".join(parts)

        if _truthy(value):
            scoped = {**context, **value} if isinstance(value, dict) else context
            return render(body, scoped)
        return ""

    previous = None
    current = template
    # Sections can nest; iterate until stable rather than writing a real parser.
    while previous != current:
        previous = current
        current = SECTION.sub(replace, current)
    return current


def _render_variables(template: str, context: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        value = _lookup(context, name)
        if value is None or value is False:
            return ""
        if value is True:
            return "true"
        return str(value)

    return VARIABLE.sub(replace, template)


def _lookup(context: dict[str, Any], path: str) -> Any:
    if path == ".":
        return context.get(".")
    node: Any = context
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


def _truthy(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, (list, dict, str)):
        return len(value) > 0
    return True
