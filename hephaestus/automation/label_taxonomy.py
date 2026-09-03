"""Repository label taxonomy for the automation provisioning CLI.

This module keeps the repository-level label contract in one place.
``hephaestus-ensure-state-labels`` provisions the automation ``state:*``
labels and the documented technical-debt labels from this taxonomy.
"""

from __future__ import annotations

from .state_labels import STATE_LABEL_SPECS

TECH_DEBT_LABEL = "tech-debt"
WONTFIX_LABEL = "wontfix"

TECH_DEBT_LABEL_SPECS: dict[str, dict[str, str]] = {
    TECH_DEBT_LABEL: {
        "color": "fbca04",
        "description": "Issue tracks technical debt for later work.",
    },
    WONTFIX_LABEL: {
        "color": "ededed",
        "description": "Issue was reviewed and will not be fixed.",
    },
}

REQUIRED_REPOSITORY_LABEL_SPECS: dict[str, dict[str, str]] = {
    **STATE_LABEL_SPECS,
    **TECH_DEBT_LABEL_SPECS,
}
