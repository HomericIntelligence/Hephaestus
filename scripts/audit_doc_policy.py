#!/usr/bin/env python3
"""Audit documentation command examples for policy violations.

Thin wrapper — delegates to hephaestus.validation.doc_policy.main().
"""

import sys

from hephaestus.validation.doc_policy import main

if __name__ == "__main__":
    sys.exit(main())
