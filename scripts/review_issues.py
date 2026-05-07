#!/usr/bin/env python3
"""Thin wrapper — delegates to hephaestus.automation.reviewer.main()."""

import sys

from hephaestus.automation.pr_reviewer import main

if __name__ == "__main__":
    sys.exit(main())
