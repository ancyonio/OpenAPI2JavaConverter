"""Derive Spring Security request rules from the spec's security requirements.

The rules are part of the contract, not business logic: the spec says which operations are
public and which scopes each one needs, so the rules are regenerated from it on every run
(see ``OpenApiSecurityRules.java.mustache``). Hand-writing them is how a public endpoint ends
up answering 401 and a write endpoint ends up accepting any valid token.

OpenAPI semantics: ``security`` is a list of alternatives (OR); each alternative is a map of
schemes that must all be satisfied (AND); each scheme lists required scopes (AND). An empty
list means public; an empty map inside the list makes anonymous access one of the options.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from o2j.models import GenerationPlan, PlannedOperation

# Scheme kinds a Spring resource server validates as a bearer JWT. Scopes become
# ``SCOPE_<scope>`` authorities through Spring Security's default JWT converter.
JWT_KINDS = frozenset({"oauth2", "openIdConnect", "http:bearer"})

ALWAYS_PUBLIC = ("/actuator/health", "/actuator/health/**", "/actuator/info")


@dataclass(frozen=True)
class SecurityRule:
    operation_id: str
    method: str
    pattern: str
    expression: str
    comment: str


def uses_jwt(plan: GenerationPlan) -> bool:
    """Whether any operation is protected by a JWT-validated scheme."""
    for op in plan.operations:
        for requirement in op.security_requirements:
            if any(plan.security_schemes.get(s) in JWT_KINDS for s in requirement):
                return True
    return False


def rules(plan: GenerationPlan) -> list[SecurityRule]:
    """One rule per operation, ordered so a literal path wins over a templated one.

    Spring evaluates matchers in order and stops at the first match, so ``/pets/mine`` must
    come before ``/pets/{id}`` or the templated rule silently governs both.
    """
    out = [
        SecurityRule(
            operation_id=op.operation_id,
            method=op.method,
            pattern=_pattern(plan.base_path, op.path),
            expression=expr,
            comment=comment,
        )
        for op in plan.operations
        for expr, comment in [_expression(op, plan.security_schemes)]
    ]
    return sorted(out, key=lambda r: (r.pattern.count("{"), r.pattern, r.method))


def _pattern(base_path: str, path: str) -> str:
    return (base_path.rstrip("/") + "/" + path.lstrip("/")) if base_path else path


def _expression(op: PlannedOperation, schemes: dict[str, str]) -> tuple[str, str]:
    """The Java authorization call for one operation, plus a comment saying why."""
    requirements = op.security_requirements
    if not requirements:
        return "permitAll()", "public: no security requirement"
    if any(not requirement for requirement in requirements):
        return "permitAll()", "public: anonymous access is one of the declared alternatives"

    alternatives: list[list[str]] = []
    for requirement in requirements:
        unsupported = sorted(s for s in requirement if schemes.get(s) not in JWT_KINDS)
        if unsupported:
            # Fail closed. An apiKey or basic scheme needs an authentication mechanism o2j
            # does not wire; until someone writes one, nothing should get through.
            return (
                "denyAll()",
                f"DENIED: scheme(s) {', '.join(unsupported)} are not wired by o2j. Add an "
                "authentication mechanism and a rule in SecurityConfig before this one",
            )
        alternatives.append(
            sorted({f"SCOPE_{scope}" for scopes in requirement.values() for scope in scopes})
        )

    described = " OR ".join(
        " AND ".join(alt) if alt else "any valid token" for alt in alternatives
    )
    if any(not alt for alt in alternatives):
        return "authenticated()", f"requires {described}"
    if all(len(alt) == 1 for alt in alternatives):
        authorities = sorted({alt[0] for alt in alternatives})
        if len(authorities) == 1:
            return f'hasAuthority("{authorities[0]}")', f"requires {described}"
        quoted = ", ".join(f'"{a}"' for a in authorities)
        return f"hasAnyAuthority({quoted})", f"requires {described}"

    def all_of(alt: list[str]) -> str:
        inner = ", ".join(f'AuthorityAuthorizationManager.hasAuthority("{a}")' for a in alt)
        return f"AuthorizationManagers.allOf({inner})"

    if len(alternatives) == 1:
        return f"access({all_of(alternatives[0])})", f"requires {described}"
    any_of = ", ".join(all_of(alt) for alt in alternatives)
    return f"access(AuthorizationManagers.anyOf({any_of}))", f"requires {described}"


def java_identifier(text: str) -> str:
    """A safe Java method-name fragment for an operationId."""
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", text)
    return cleaned if cleaned[:1].isalpha() or cleaned[:1] == "_" else f"op_{cleaned}"


def sample_path(rule_pattern: str) -> str:
    """A concrete request path for a pattern: every template variable becomes a fixed id."""
    return re.sub(r"\{[^}]+\}", "o2j-test", rule_pattern)
