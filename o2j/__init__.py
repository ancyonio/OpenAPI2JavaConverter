"""O2J - OpenAPI YAML to Java Spring Boot conversion factory.

The package is organised so that every capability has exactly one implementation in
``o2j.stages`` and two thin adapters over it: ``o2j.cli`` for humans and CI, and
``o2j.mcp_server`` for GitHub Copilot agents. Logic must never live in an adapter.
"""

__version__ = "1.0.0"
