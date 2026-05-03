#!/usr/bin/env python3
"""Thin wrapper — delegates to hephaestus.automation.ci_driver.main()."""

import sys

from hephaestus.automation.ci_driver import main

if __name__ == "__main__":
    sys.exit(main())
