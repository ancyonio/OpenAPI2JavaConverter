"""Pipeline stages.

Every capability the factory offers is implemented here exactly once. ``o2j.cli`` and
``o2j.mcp_server`` are adapters over these functions and hold no logic of their own, so the
CLI and the Copilot tools can never drift apart.
"""
