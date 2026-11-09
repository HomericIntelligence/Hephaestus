"""Shared git-lifecycle constants.

Holds the state-directory constant shared by the git-lifecycle operations and
their product-layer callers. The module imports nothing, so importing it does
not eagerly load any other ``hephaestus.git`` module.
"""

DEFAULT_STATE_DIR: str = "build/.issue_implementer"
