"""Thin gh CLI subprocess wrapper with rate-limit retry.

This module is intentionally a leaf: it may only import from stdlib and
other ``hephaestus.github`` modules.  It must NOT import from
``hephaestus.automation`` — doing so creates a circular import cycle
because ``hephaestus.automation.github_api`` imports from this package.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time

from hephaestus.github.rate_limit import detect_claude_usage_limit, detect_rate_limit, wait_until
from hephaestus.utils.helpers import run_subprocess

logger = logging.getLogger(__name__)


def _gh_call(
    args: list[str],
    check: bool = True,
    retry_on_rate_limit: bool = True,
    max_retries: int = 3,
) -> subprocess.CompletedProcess[str]:
    """Call gh CLI with rate limit handling.

    Args:
        args: Arguments to pass to gh
        check: Whether to raise on non-zero exit
        retry_on_rate_limit: Whether to retry on rate limit
        max_retries: Maximum retry attempts

    Returns:
        CompletedProcess instance

    Raises:
        subprocess.CalledProcessError: If command fails and check=True
        RuntimeError: If Claude usage limit detected

    """
    for attempt in range(max_retries):
        try:
            result = run_subprocess(
                ["gh", *args],
                check=check,
                timeout=120,  # 2 minute timeout for gh CLI calls
            )
            return result
        except subprocess.CalledProcessError as e:
            stderr = e.stderr if e.stderr else ""

            if detect_claude_usage_limit(stderr):
                raise RuntimeError(
                    "Claude API usage limit reached. Please check your billing."
                ) from e

            reset_epoch = detect_rate_limit(stderr)
            if reset_epoch is not None:
                if retry_on_rate_limit:
                    if reset_epoch > 0:
                        wait_until(reset_epoch)
                    else:
                        wait_seconds = min(60 * (2**attempt), 300)
                        logger.warning(f"Rate limited but no reset time, waiting {wait_seconds}s")
                        time.sleep(wait_seconds)
                    continue
                else:
                    raise RuntimeError(
                        f"GitHub API rate limit reached. Reset at epoch {reset_epoch}"
                    ) from e

            non_transient_patterns = [
                r"403|forbidden|permission denied",
                r"404|not found",
                r"400|bad request",
                r"401|unauthorized",
                r"invalid argument",
            ]
            if any(re.search(pattern, stderr, re.IGNORECASE) for pattern in non_transient_patterns):
                logger.error(f"Non-transient error detected: {stderr[:200]}")
                raise

            if attempt == max_retries - 1:
                raise

            wait_seconds = 2**attempt
            logger.warning(f"gh call failed (attempt {attempt + 1}), retrying in {wait_seconds}s")
            time.sleep(wait_seconds)

    raise RuntimeError("gh call failed after all retries")
