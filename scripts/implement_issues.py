#!/usr/bin/env python3
"""Thin wrapper — delegates to hephaestus.automation.implementer.main()."""

import sys

from hephaestus.automation.implementer import main

if __name__ == "__main__":
    sys.exit(main())
