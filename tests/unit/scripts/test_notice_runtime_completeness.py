"""NOTICE must document every base runtime dependency (issue #1218).

Scope: this guard covers [project].dependencies only. Optional extras are
documented in a separate NOTICE section ("Optional (extras) runtime
dependencies") and are intentionally NOT checked here.
"""

import re
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

_OPERATORS = ("<=", ">=", "==", "!=", "~=", "<", ">", "=")


def _dist_name(spec: str) -> str:
    """Bare PEP 508 distribution name from a dependency spec.

    Mirrors ``_find_dep`` in ``test_dependency_floor_consistency.py``: strip the
    environment marker, then the version operator, then PEP 503-normalize.

    Args:
        spec: A PEP 508 dependency specifier, e.g.
            ``"tzdata; platform_system == 'Windows'"``.

    Returns:
        The PEP 503-normalized distribution name (lowercase; runs of ``-``,
        ``_``, ``.`` collapsed to a single ``-``).

    """
    head = spec.split(";", 1)[0].strip()  # drop ' ; platform_system == ...'
    for sep in _OPERATORS:
        if sep in head:
            head = head.split(sep, 1)[0].strip()
            break
    return re.sub(r"[-_.]+", "-", head).lower()  # PEP 503 normalization


def _notice_runtime_block(notice_text: str) -> str:
    """Return the runtime-dependencies block of NOTICE.

    The block is the text between the "Third-party runtime dependencies" header
    and the next ``====`` rule.

    Args:
        notice_text: The full contents of the NOTICE file.

    Returns:
        The runtime-dependencies block text.

    Raises:
        AssertionError: If the runtime-dependencies header cannot be found.

    """
    header = "Third-party runtime dependencies"
    start = notice_text.find(header)
    assert start != -1, "NOTICE runtime-dependencies section header not found"
    # Skip past the header line AND its underline ``====`` rule to the body,
    # so the rule that terminates the body is the *next* one, not the header's.
    body_start = notice_text.index("\n", start) + 1
    rest = notice_text[body_start:]
    underline = re.match(r"={5,}\s*\n", rest)
    if underline:
        rest = rest[underline.end() :]
    end_rule = re.search(r"^={5,}\s*$", rest, re.MULTILINE)
    block = rest[: end_rule.start()] if end_rule else rest
    return block


def test_notice_documents_every_runtime_dependency() -> None:
    """Assert every base runtime dependency is documented in NOTICE.

    Each distribution in ``[project].dependencies`` must appear in NOTICE's
    "Third-party runtime dependencies" block.
    """
    repo_root = Path(__file__).resolve().parents[3]
    with (repo_root / "pyproject.toml").open("rb") as f:
        deps = tomllib.load(f)["project"]["dependencies"]
    notice = (repo_root / "NOTICE").read_text(encoding="utf-8")

    block = _notice_runtime_block(notice)
    assert block.strip(), "NOTICE runtime-dependencies block is empty"
    # Normalize every whitespace-separated token in the block for matching.
    block_tokens = {re.sub(r"[-_.]+", "-", tok).lower() for tok in block.split()}

    missing = [spec for spec in deps if _dist_name(spec) not in block_tokens]
    assert not missing, (
        "NOTICE 'Third-party runtime dependencies' omits: "
        f"{[_dist_name(s) for s in missing]} "
        "(declared in pyproject.toml [project].dependencies)"
    )
