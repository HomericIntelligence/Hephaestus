#!/usr/bin/env python3
"""Thin wrapper — delegates to hephaestus.automation.plan_reviewer.main()."""

import sys

from hephaestus.automation.plan_reviewer import main

if __name__ == "__main__":
    sys.exit(main())
