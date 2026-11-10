"""Validation rules.

``semantic`` holds the checks that catch specs which are schema-valid but generate wrong
Java; ``naming`` holds the house REST conventions. Both return plain ``Finding`` lists so
they compose in any order.
"""

from o2j.rules import naming, semantic

__all__ = ["naming", "semantic"]
