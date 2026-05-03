#!/usr/bin/env python3
"""Thin wrapper — delegates to hephaestus.automation.address_review.main()."""

import sys

from hephaestus.automation.address_review import main

if __name__ == "__main__":
    sys.exit(main())
