"""Functional contracts for the Athena agent-contract v1 adoption (issue #2853).

These tests assert *behavior*, not document prose: the root documents are
bounded regular UTF-8 files, root AGENTS.md carries exactly one generated
Athena development-principles block that matches the released projection, root
CLAUDE.md is the exact ``@AGENTS.md`` pointer, the ``_required.yml`` workflow
owns one minimal provider job with aggregate fan-in, and the #2811 safety
checkpoints reference enabled plugin skills with the resolved conditional
gate form.

The byte-equality arbiter for the generated block is the released Athena
validator (the ``agent-contract`` reusable-workflow job) plus the pinned
projection digest below. The tests keep no copy of Athena's catalog text:
renamed, reworded, and relinked rows fail through the projection digest, and
identifier-set defects fail through the expected identifier tuple.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]

CONTRACT_TAG = "agent-contract-v1.0.0"
# SHA-256 of docs/principles/README.md at the protected signed provider tag.
# Bound by the immutable provider release 384248657 and re-verified at
# implementation start (issue #2853, plan F-001).
CATALOG_SHA256 = "c79a2824661546182b6fbc0541ca5bd08b666644e74590bf92c7a504abc022b3"
# SHA-256 of the marked block (begin marker through end marker) projected from
# the pinned released catalog with the released row and render contract.
# Cross-checked byte-equal against the released render_principles_block.
EXPECTED_BLOCK_SHA256 = "54705687c9c8d7401127622340c9989ce2579c1f6340caac79ab08d2410ad0be"

BEGIN_MARKER = f"<!-- BEGIN ATHENA DEVELOPMENT PRINCIPLES: {CONTRACT_TAG} -->"
END_MARKER = "<!-- END ATHENA DEVELOPMENT PRINCIPLES -->"
DETAIL_URL_PREFIX = (
    f"https://github.com/HomericIntelligence/Athena/blob/{CONTRACT_TAG}/docs/principles/"
)
CONTRACT_USES = f"HomericIntelligence/Athena/.github/workflows/_agent-contract.yml@{CONTRACT_TAG}"
# Released root-document cap (scripts/policies/agent_contract.py at the tag).
MAX_ROOT_DOCUMENT_BYTES = 256 * 1024
POINTER_BYTES = b"@AGENTS.md\n"
EXPECTED_PRINCIPLE_IDENTIFIERS = tuple(f"P{number:03d}" for number in range(1, 92))
EXPECTED_BLOCK_ROWS = 91

# Exact legacy pointer-file value, recorded for the legacy inventory. The two
# legacy obligations survive in the new value: the pointer routes the reader
# to the root contract, and the root contract keeps sole authority.
LEGACY_CLAUDE_MD = (
    "# Claude Code guidance\n"
    "\n"
    "Follow [`AGENTS.md`](AGENTS.md). "
    "It is the sole authoritative agent contract for this repository.\n"
)

ROW_RE = re.compile(
    r"\A- \[(?P<identifier>P[0-9]{3}) — (?P<name>[^\]\r\n]+)\]"
    r"\((?P<url>[^)\r\n]+)\) — (?P<description>.+)\Z"
)

ALLOWED_CLAUDE_REFERENCE_LINES = {
    Path(".github/CODEOWNERS"): {"CLAUDE.md @mvillmow"},
    Path("docs/adr/0001-automation-library-boundary.md"): {
        "At decision time this guidance lived in `CLAUDE.md`; "
        "it is now consolidated in `AGENTS.md`."
    },
}

# Preserve the exact external review record without excluding other fixture content.
HISTORICAL_FIXTURE_DIGESTS = {
    Path("tests/fixtures/review-exchange/scylla-2093-state-carrier.txt"): (
        "54b1cdff9e8113fd6af7bf2a5ec6c8ac50279e8f39973963e3904ca2d911e79a"
    ),
}

EXCLUDED_PARTS = {
    ".git",
    ".hypothesis",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tmp",
    ".venv",
    ".worktrees",
    "build",
}


def _read_root_document(root: Path, name: str) -> tuple[str | None, str | None]:
    """Read one bounded regular UTF-8 root document or return a rejection.

    The typed and bounded gate runs before any generated-block processing:
    symbolic links, non-regular files, oversized files, and invalid UTF-8 are
    all rejected here.
    """
    path = root / name
    if path.is_symlink():
        return None, "must not be a symbolic link"
    try:
        metadata = path.stat()
    except OSError as error:
        return None, f"is missing or unreadable: {error}"
    if not stat.S_ISREG(metadata.st_mode):
        return None, "must be a regular file"
    if metadata.st_size > MAX_ROOT_DOCUMENT_BYTES:
        return None, f"must not exceed {MAX_ROOT_DOCUMENT_BYTES} bytes"
    try:
        data = path.read_bytes()
    except OSError as error:
        return None, f"is missing or unreadable: {error}"
    try:
        return data.decode("utf-8"), None
    except UnicodeError as error:
        return None, f"must be readable UTF-8: {error}"


def _load_workflow(path: Path) -> dict[str, Any]:
    """Load workflow YAML without YAML 1.1 coercing the ``on`` key to true."""
    return cast(
        dict[str, Any],
        yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader),
    )


def _marker_indexes(lines: list[str], marker: str) -> list[int]:
    """Return the indexes of standalone marker lines (exact line match)."""
    return [index for index, line in enumerate(lines) if line == marker]


def _detail_link_violations(matches: list[re.Match[str]]) -> list[str]:
    """Return every detail-link rule violation in the parsed rows."""
    violations: list[str] = []
    for match in matches:
        url = match.group("url")
        detail = url.removeprefix(DETAIL_URL_PREFIX)
        if not url.startswith(DETAIL_URL_PREFIX) or not re.fullmatch(
            r"details/p[0-9]{3}-[a-z0-9-]+\.md", detail
        ):
            violations.append("detail-link-prefix-mismatch")
        elif not detail.startswith(f"details/{match.group('identifier').casefold()}-"):
            violations.append("detail-link-identifier-mismatch")
    return violations


def _generated_block_violations(text: str) -> list[str]:
    """Return every generated-block rule violation in one document.

    Violation codes are stable strings: marker presence, count, and order,
    blank lines inside the marked block, row shape, identifier tuple, detail
    link prefix and identifier linkage, and the projection digest.
    """
    lines = text.split("\n")
    begins = _marker_indexes(lines, BEGIN_MARKER)
    ends = _marker_indexes(lines, END_MARKER)
    violations: list[str] = []
    if not begins:
        violations.append("begin-marker-missing")
    if not ends:
        violations.append("end-marker-missing")
    if len(begins) > 1:
        violations.append("begin-marker-duplicate")
    if len(ends) > 1:
        violations.append("end-marker-duplicate")
    if violations:
        # A malformed marker envelope blocks every row-level check.
        return violations
    begin, end = begins[0], ends[0]
    if begin >= end:
        return ["markers-out-of-order"]

    rows = lines[begin + 1 : end]
    if any(row == "" for row in rows):
        violations.append("blank-line-in-block")
    parsed: list[re.Match[str] | None] = [ROW_RE.fullmatch(row) for row in rows]
    if any(match is None for match in parsed):
        violations.append("row-malformed")
        return violations
    matches = [cast(re.Match[str], match) for match in parsed]

    identifiers = [match.group("identifier") for match in matches]
    if tuple(identifiers) != EXPECTED_PRINCIPLE_IDENTIFIERS:
        violations.append("identifier-order-mismatch")

    violations.extend(_detail_link_violations(matches))

    block = "\n".join(lines[begin : end + 1])
    if hashlib.sha256(block.encode("utf-8")).hexdigest() != EXPECTED_BLOCK_SHA256:
        violations.append("projection-digest-mismatch")
    return violations


def _synthetic_rows() -> list[str]:
    """Build 91 fabricated rows in the exact released row shape.

    The content is synthetic on purpose: it exercises the block rules without
    copying catalog text into the tests.
    """
    return [
        f"- [{identifier} — Synthetic Rule]({DETAIL_URL_PREFIX}details/"
        f"{identifier.casefold()}-synthetic-rule.md) — Follow the synthetic rule."
        for identifier in EXPECTED_PRINCIPLE_IDENTIFIERS
    ]


def _synthetic_document(rows: list[str]) -> str:
    """Render one document that carries one marked block from the rows."""
    return "\n".join(("# Fixture", "", BEGIN_MARKER, *rows, END_MARKER, ""))


def _find_unexpected_claude_references(repo_root: Path, test_file: Path) -> list[str]:
    """Return unauthorized legacy-contract references below a source root."""
    unexpected: list[str] = []
    for root, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [dirname for dirname in dirnames if dirname not in EXCLUDED_PARTS]
        for filename in filenames:
            path = Path(root) / filename
            if path.resolve() == test_file.resolve():
                continue
            relative = path.relative_to(repo_root)
            content = path.read_bytes()
            if (
                relative in HISTORICAL_FIXTURE_DIGESTS
                and hashlib.sha256(content).hexdigest() == HISTORICAL_FIXTURE_DIGESTS[relative]
            ):
                continue
            try:
                lines = content.decode("utf-8").splitlines()
            except UnicodeDecodeError:
                continue

            allowed = ALLOWED_CLAUDE_REFERENCE_LINES.get(relative, set())
            for number, line in enumerate(lines, 1):
                if "CLAUDE.md" in line and line.strip() not in allowed:
                    unexpected.append(f"{relative}:{number}: {line.strip()}")
    return unexpected


# ---------------------------------------------------------------------------
# Typed and bounded root-document gate
# ---------------------------------------------------------------------------


def _typed_gate_case(tmp_path: Path, name: str, content: bytes) -> Path:
    """Write one regular-file gate fixture."""
    candidate = tmp_path / name
    candidate.write_bytes(content)
    return candidate


@pytest.mark.parametrize(
    ("identifier", "reason_part"),
    [
        ("missing", "missing or unreadable"),
        ("symlink", "symbolic link"),
        ("non-regular", "regular file"),
        ("oversized", "exceed"),
        ("invalid-utf8", "readable UTF-8"),
    ],
)
def test_typed_gate_rejects_input_before_block_processing(
    tmp_path: Path, identifier: str, reason_part: str
) -> None:
    """Missing, symlinked, non-regular, oversized, and malformed files fail."""
    if identifier == "symlink":
        target = _typed_gate_case(tmp_path, "target.md", b"real\n")
        os.symlink(target, tmp_path / "AGENTS.md")
    elif identifier == "non-regular":
        (tmp_path / "AGENTS.md").mkdir()
    elif identifier == "oversized":
        _typed_gate_case(tmp_path, "AGENTS.md", b"a" * (MAX_ROOT_DOCUMENT_BYTES + 1))
    elif identifier == "invalid-utf8":
        _typed_gate_case(tmp_path, "AGENTS.md", b"\xff\xfe invalid \x80\n")

    text, rejection = _read_root_document(tmp_path, "AGENTS.md")

    assert text is None
    assert rejection is not None and reason_part in rejection


def test_typed_gate_accepts_the_exact_byte_boundary(tmp_path: Path) -> None:
    """A root document at exactly the released cap passes the size gate."""
    expected = (b"a" * (MAX_ROOT_DOCUMENT_BYTES - 1)) + b"\n"
    _typed_gate_case(tmp_path, "AGENTS.md", expected)

    text, rejection = _read_root_document(tmp_path, "AGENTS.md")

    assert rejection is None
    assert text is not None and len(text.encode("utf-8")) == MAX_ROOT_DOCUMENT_BYTES


def test_typed_gate_rejects_before_marker_handling(tmp_path: Path) -> None:
    """A typed rejection takes priority over any generated-block defect."""
    bad = b"\xff\xfe no markers here\n"
    _typed_gate_case(tmp_path, "AGENTS.md", bad)

    text, rejection = _read_root_document(tmp_path, "AGENTS.md")

    assert text is None
    assert rejection is not None and "readable UTF-8" in rejection
    assert "marker" not in rejection


# ---------------------------------------------------------------------------
# Generated-block shape on synthetic fixtures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("identifier", "mutate", "expected_violations"),
    [
        (
            "missing-block",
            lambda rows: "# Fixture\n\nNo markers here.\n",
            {"begin-marker-missing", "end-marker-missing"},
        ),
        ("begin-only", lambda rows: "\n".join(("# Fixture", BEGIN_MARKER)), {"end-marker-missing"}),
        ("end-only", lambda rows: "\n".join(("# Fixture", END_MARKER)), {"begin-marker-missing"}),
        (
            "reversed-markers",
            lambda rows: "\n".join(("# Fixture", END_MARKER, *rows, BEGIN_MARKER)),
            {"markers-out-of-order"},
        ),
        (
            "duplicate-block",
            lambda rows: _synthetic_document(rows) + _synthetic_document(rows),
            {"begin-marker-duplicate", "end-marker-duplicate"},
        ),
        (
            "blank-line-after-begin",
            lambda rows: "\n".join(("# Fixture", BEGIN_MARKER, "", *rows, END_MARKER)),
            {"blank-line-in-block"},
        ),
        (
            "blank-line-before-end",
            lambda rows: "\n".join(("# Fixture", BEGIN_MARKER, *rows, "", END_MARKER)),
            {"blank-line-in-block"},
        ),
        (
            "missing-row",
            lambda rows: _synthetic_document(rows[:-1]),
            {"identifier-order-mismatch"},
        ),
        (
            "extra-row",
            lambda rows: _synthetic_document([*rows, rows[-1]]),
            {"identifier-order-mismatch"},
        ),
        (
            "duplicate-identifier",
            lambda rows: _synthetic_document([rows[0], *rows[2:]]),
            {"identifier-order-mismatch"},
        ),
        (
            "reordered-rows",
            lambda rows: _synthetic_document([rows[1], rows[0], *rows[2:]]),
            {"identifier-order-mismatch"},
        ),
        (
            "malformed-row",
            lambda rows: _synthetic_document(["[broken", *rows[1:]]),
            {"row-malformed"},
        ),
    ],
)
def test_generated_block_negative_rows(
    identifier: str, mutate: Any, expected_violations: set[str]
) -> None:
    """Each structural block defect produces its specific violation code."""
    document = mutate(_synthetic_rows())

    violations = _generated_block_violations(document)

    assert expected_violations <= set(violations), (identifier, violations)


def test_renamed_row_breaks_projection() -> None:
    """A renamed principle cannot keep the released projection digest."""
    rows = _synthetic_rows()
    rows[0] = rows[0].replace("Synthetic Rule", "Renamed Synthetic Rule")

    violations = _generated_block_violations(_synthetic_document(rows))

    assert "projection-digest-mismatch" in violations


def test_reworded_row_breaks_projection() -> None:
    """A reworded decision rule cannot keep the released projection digest."""
    rows = _synthetic_rows()
    rows[0] = rows[0] + " Obey it twice."

    violations = _generated_block_violations(_synthetic_document(rows))

    assert "projection-digest-mismatch" in violations


def test_relinked_row_breaks_projection() -> None:
    """A relinked row cannot keep the released projection or link prefix."""
    rows = _synthetic_rows()
    rows[0] = rows[0].replace(f"blob/{CONTRACT_TAG}/", "blob/main/")

    violations = _generated_block_violations(_synthetic_document(rows))

    assert "projection-digest-mismatch" in violations
    assert "detail-link-prefix-mismatch" in violations


# ---------------------------------------------------------------------------
# Live document contracts
# ---------------------------------------------------------------------------


def test_agents_md_is_bounded_regular_utf8() -> None:
    """The live root contract passes the typed and bounded gate."""
    text, rejection = _read_root_document(REPO_ROOT, "AGENTS.md")

    assert rejection is None
    assert text is not None


def test_agents_md_carries_exactly_one_generated_block() -> None:
    """Exactly one begin marker and one end marker stand alone, in order."""
    text, rejection = _read_root_document(REPO_ROOT, "AGENTS.md")
    assert rejection is None and text is not None
    lines = text.split("\n")

    begins = _marker_indexes(lines, BEGIN_MARKER)
    ends = _marker_indexes(lines, END_MARKER)

    assert len(begins) == 1 and len(ends) == 1
    assert begins[0] < ends[0]


def test_generated_block_identifiers_exact_projection() -> None:
    """Rows are exactly P001 through P091, unique and ordered."""
    text, rejection = _read_root_document(REPO_ROOT, "AGENTS.md")
    assert rejection is None and text is not None

    violations = _generated_block_violations(text)

    assert "identifier-order-mismatch" not in violations, violations


def test_generated_block_row_contract() -> None:
    """No blank line sits inside the block; each row obeys the row contract."""
    text, rejection = _read_root_document(REPO_ROOT, "AGENTS.md")
    assert rejection is None and text is not None

    violations = _generated_block_violations(text)

    assert "blank-line-in-block" not in violations, violations
    assert "row-malformed" not in violations, violations
    assert "detail-link-prefix-mismatch" not in violations, violations
    assert "detail-link-identifier-mismatch" not in violations, violations
    lines = text.split("\n")
    begin = _marker_indexes(lines, BEGIN_MARKER)[0]
    end = _marker_indexes(lines, END_MARKER)[0]
    assert end - begin - 1 == EXPECTED_BLOCK_ROWS


def test_generated_block_matches_released_projection() -> None:
    """The marked block byte-matches the pinned released projection."""
    text, rejection = _read_root_document(REPO_ROOT, "AGENTS.md")
    assert rejection is None and text is not None

    violations = _generated_block_violations(text)

    assert "projection-digest-mismatch" not in violations, violations


def test_claude_md_pointer_bytes() -> None:
    """Root CLAUDE.md is a regular file with the exact required bytes."""
    path = REPO_ROOT / "CLAUDE.md"

    assert not path.is_symlink()
    metadata = path.stat()
    assert stat.S_ISREG(metadata.st_mode)
    assert path.read_bytes() == POINTER_BYTES


def test_legacy_claude_md_inventory() -> None:
    """The recorded legacy value holds; both legacy obligations survive."""
    assert len(LEGACY_CLAUDE_MD.encode("utf-8")) == 122
    assert "Follow [`AGENTS.md`](AGENTS.md)." in LEGACY_CLAUDE_MD

    # Obligation 1: the exact pointer routes the reader to the root contract.
    pointer_target = REPO_ROOT / POINTER_BYTES.decode("utf-8").strip().removeprefix("@")
    assert pointer_target.resolve() == (REPO_ROOT / "AGENTS.md").resolve()

    # Obligation 2: the root contract keeps sole repository-contract authority.
    agents_text, rejection = _read_root_document(REPO_ROOT, "AGENTS.md")
    assert rejection is None and agents_text is not None
    opening = "\n".join(agents_text.split("\n")[:8])
    assert "single authoritative agent contract" in opening


# ---------------------------------------------------------------------------
# _required.yml workflow wiring
# ---------------------------------------------------------------------------


def _required_workflow() -> dict[str, Any]:
    return _load_workflow(REPO_ROOT / ".github" / "workflows" / "_required.yml")


def test_required_workflow_agent_contract_job_is_minimal() -> None:
    """The provider job is one read-only reusable call with no execution."""
    job = _required_workflow()["jobs"]["agent-contract"]

    assert job["uses"] == CONTRACT_USES
    assert job["permissions"] == {"contents": "read"}
    forbidden = {"with", "secrets", "if", "steps", "runs-on", "strategy", "name"}
    assert forbidden.isdisjoint(job)


def test_required_workflow_required_event_paths() -> None:
    """Pull-request, exact-main push, and merge-group triggers stay intact."""
    triggers = _required_workflow()["on"]

    assert set(triggers["pull_request"]["types"]) == {
        "opened",
        "synchronize",
        "edited",
        "reopened",
        "ready_for_review",
    }
    assert triggers["push"]["branches"] == ["main"]
    assert triggers["merge_group"]["types"] == ["checks_requested"]


def test_required_workflow_aggregate_fan_in() -> None:
    """The aggregate census and the runtime expected-job list gate the contract."""
    workflow = _required_workflow()
    gate = workflow["jobs"]["required-checks-gate"]
    expected_env = json.loads(gate["steps"][0]["env"]["EXPECTED_JOBS"])

    assert "agent-contract" in gate["needs"]
    assert "agent-contract" in expected_env


# ---------------------------------------------------------------------------
# #2811 safety-checkpoint binding (plan F-003)
# ---------------------------------------------------------------------------


def _safety_checkpoint_section() -> list[str]:
    """Return the ``Agent safety checkpoints`` section lines."""
    agents_text, rejection = _read_root_document(REPO_ROOT, "AGENTS.md")
    assert rejection is None and agents_text is not None
    lines = agents_text.split("\n")
    heading_indexes = [
        index for index, line in enumerate(lines) if line == "## Agent safety checkpoints"
    ]
    assert len(heading_indexes) == 1, "section must exist exactly once"
    start = heading_indexes[0]
    next_heading = next(
        (index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    return lines[start + 1 : next_heading]


def _checkpoint_rows(section: list[str]) -> list[str]:
    """Rebuild each checkpoint bullet from its wrapped continuation lines."""
    rows: list[str] = []
    for line in section:
        if line.startswith("- "):
            rows.append(line)
        elif rows and line.startswith("  ") and line.strip():
            rows[-1] = f"{rows[-1]} {line.strip()}"
    return rows


def test_safety_checkpoint_section_exists_once() -> None:
    """The #2811 contract surface has exactly one safety-checkpoint section."""
    agents_text, rejection = _read_root_document(REPO_ROOT, "AGENTS.md")
    assert rejection is None and agents_text is not None

    assert agents_text.split("\n").count("## Agent safety checkpoints") == 1


def test_safety_checkpoint_references_enabled_plugins() -> None:
    """Each checkpoint row names a plugin that .claude/settings.json enables."""
    settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    enabled = set(settings["enabledPlugins"])
    assert enabled
    referenced = {
        match.removeprefix("/athena:").removeprefix("athena:")
        for match in re.findall(r"/?athena:[a-z-]+", "\n".join(_safety_checkpoint_section()))
    }
    required = {"myrmidon-swarm", "finish-branch", "code-review", "skill-advisor"}

    assert required <= referenced
    assert {f"{skill}@Athena" for skill in referenced} <= enabled


def test_safety_checkpoint_conditional_gate_form() -> None:
    """Each row keeps the resolved guard, safe-work, and confirm clauses."""
    rows = _checkpoint_rows(_safety_checkpoint_section())
    swarm_row = next(row for row in rows if "myrmidon-swarm" in row)
    confirm_row = next(row for row in rows if "finish-branch" in row)

    assert "If host policy or task scope requires approval" in swarm_row
    assert "Start safe work that is in scope" in swarm_row
    assert "after you obtain all required approvals" in swarm_row
    assert "explicit confirm steps" in confirm_row


# ---------------------------------------------------------------------------
# Stale-consumer scan (preserved pre-#2853 contracts)
# ---------------------------------------------------------------------------


def test_agent_contract_scan_excludes_foreign_worktree_roots(tmp_path: Path) -> None:
    """A foreign ignored worktree cannot add policy references to this checkout."""
    foreign = tmp_path / ".worktrees" / "codex" / "branch" / "policy.md"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("Use CLAUDE.md here.\n", encoding="utf-8")

    assert _find_unexpected_claude_references(tmp_path, Path(__file__)) == []


def test_agent_contract_scan_keeps_untracked_source_candidates(tmp_path: Path) -> None:
    """An ordinary untracked source file remains in the policy scan."""
    candidate = tmp_path / "docs" / "policy.md"
    candidate.parent.mkdir()
    candidate.write_text("Use CLAUDE.md here.\n", encoding="utf-8")

    assert _find_unexpected_claude_references(tmp_path, Path(__file__)) == [
        "docs/policy.md:1: Use CLAUDE.md here."
    ]


def test_only_explicit_compatibility_and_history_references_remain() -> None:
    """No live policy consumer may continue citing the legacy pointer."""
    assert _find_unexpected_claude_references(REPO_ROOT, Path(__file__)) == []


@pytest.mark.parametrize(
    ("destination", "changed", "unexpected"),
    [
        ("tests/fixtures/review-exchange/scylla-2093-state-carrier.txt", False, False),
        ("tests/fixtures/review-exchange/scylla-2093-state-carrier.txt", True, True),
        ("tests/fixtures/review-exchange/other.txt", False, True),
        ("docs/policy.txt", False, True),
    ],
)
def test_historical_fixture_exception_requires_exact_path_and_content(
    tmp_path: Path, destination: str, changed: bool, unexpected: bool
) -> None:
    """Exclude only unchanged historical evidence at its registered path."""
    source = REPO_ROOT / "tests/fixtures/review-exchange/scylla-2093-state-carrier.txt"
    content = source.read_bytes()
    target = tmp_path / destination
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content + (b"\nChanged historical evidence.\n" if changed else b""))

    findings = _find_unexpected_claude_references(tmp_path, Path(__file__))

    assert bool(findings) is unexpected
    if unexpected:
        assert all(finding.startswith(f"{destination}:") for finding in findings)
