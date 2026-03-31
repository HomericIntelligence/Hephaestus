#!/usr/bin/env python3
"""Check tier label consistency in markdown files.

Thin wrapper — delegates to hephaestus.validation.tier_labels.main().
"""

import sys

from hephaestus.validation.tier_labels import main

if __name__ == "__main__":
    sys.exit(main())
