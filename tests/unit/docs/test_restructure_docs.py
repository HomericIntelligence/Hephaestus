"""Document-content guards for the #3504 restructure documentation (issue #3525).

Each test reads only the files it owns and runs no subprocess. The guards
check that living documents agree with the tree: the ``AGENTS.md`` structure
map names the real subpackages, the ADR-0001 layer list names ``git``, the
``docs/architecture.md`` relative links resolve, the ``pdoc`` target set
matches the ``justfile`` recipe, future paths in prose name their owning
issue, and the section-11 future-path paragraph sits below the facade table.
"""

from __future__ import annotations

import re
from pathlib import Path

from hephaestus.validation.api_reference import expected_pdoc_targets

REPO_ROOT = Path(__file__).resolve().parents[3]

# Library subpackages named by ADR-0001 at the base commit. The layer list
# must keep every one of them; ``git`` joins the list in #3525.
ADR_0001_BASE_LIBRARY_MEMBERS = frozenset(
    {
        "utils",
        "io",
        "config",
        "logging",
        "cli",
        "system",
        "github",
        "validation",
        "resilience",
        "markdown",
        "ci",
        "benchmarks",
        "datasets",
        "discovery",
        "forensics",
        "nats",
        "version",
        "agents",
        "scripts_lib",
    }
)

_STRUCTURE_HEADING = "## Repository Structure"
_HEPHAESTUS_ROW = re.compile(r"^[├└]── hephaestus/")
_TOP_LEVEL_ROW = re.compile(r"^[├└]── ")
_SUBPACKAGE_ROW = re.compile(r"^│   [├└]── ([a-z_]+)/")
_FUTURE_PATH = re.compile(r"hephaestus/([a-z_]+)/")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")
_LINK_TARGET = re.compile(r"\]\(([^)]*)\)")


def _read(repo_relative: str) -> str:
    """Return the text of one repository file."""
    return (REPO_ROOT / repo_relative).read_text(encoding="utf-8")


def _structure_fence_rows() -> list[str]:
    """Return the lines inside the AGENTS.md repository-structure fence."""
    lines = _read("AGENTS.md").splitlines()
    heading = next(
        (index for index, line in enumerate(lines) if line.strip() == _STRUCTURE_HEADING),
        None,
    )
    assert heading is not None, "AGENTS.md lost the '## Repository Structure' heading"
    fence = next(
        (index for index in range(heading, len(lines)) if lines[index].startswith("```")),
        None,
    )
    assert fence is not None, "AGENTS.md lost the repository-structure code fence"
    rows: list[str] = []
    for line in lines[fence + 1 :]:
        if line.startswith("```"):
            return rows
        rows.append(line)
    return rows


def _structure_map_names() -> set[str]:
    """Collect the first-level ``hephaestus/<name>/`` rows from the fence."""
    names: set[str] = set()
    in_package_block = False
    for line in _structure_fence_rows():
        if _HEPHAESTUS_ROW.match(line):
            in_package_block = True
            continue
        if _TOP_LEVEL_ROW.match(line):
            in_package_block = False
            continue
        match = _SUBPACKAGE_ROW.match(line)
        if in_package_block and match is not None:
            names.add(match.group(1))
    return names


def _on_disk_subpackages() -> set[str]:
    """Return the Python subpackage directories under ``hephaestus/``."""
    package_root = REPO_ROOT / "hephaestus"
    return {
        path.name
        for path in package_root.iterdir()
        if path.is_dir()
        and (path / "__init__.py").is_file()
        and not path.name.startswith((".", "_"))
    }


def test_agents_structure_map_names_each_subpackage() -> None:
    """The AGENTS.md structure map and the subpackage tree agree."""
    documented = _structure_map_names()
    on_disk = _on_disk_subpackages()
    assert documented == on_disk, (
        f"AGENTS.md structure map does not match the tree: "
        f"missing_from_doc={sorted(on_disk - documented)}, "
        f"missing_on_disk={sorted(documented - on_disk)}"
    )


def test_adr_0001_layer_list_names_every_library_subpackage() -> None:
    """The ADR-0001 library list names ``git`` and keeps every base member."""
    source = _read("docs/adr/0001-automation-library-boundary.md")
    match = re.search(r"hephaestus\.\{([^}]*)\}", source, re.DOTALL)
    assert match is not None, "ADR-0001 no longer states the library layer as hephaestus.{...}"
    members = {part.strip() for part in match.group(1).split(",")} - {""}
    assert "git" in members, "ADR-0001 library list does not name git"
    missing = ADR_0001_BASE_LIBRARY_MEMBERS - members
    assert not missing, f"ADR-0001 library list dropped base members: {sorted(missing)}"


def test_architecture_doc_links_resolve() -> None:
    """Every relative Markdown link in docs/architecture.md names a real file."""
    source = _read("docs/architecture.md")
    docs_dir = REPO_ROOT / "docs"
    broken: list[str] = []
    for match in re.finditer(r"\[[^\]]*\]\(([^)]+)\)", source):
        target = match.group(1).strip()
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        path_part = target.split("#", 1)[0]
        if not path_part:
            continue
        if not (docs_dir / path_part).exists():
            broken.append(target)
    assert not broken, f"docs/architecture.md has unresolvable relative links: {broken}"


def _justfile_docs_pdoc_tokens() -> set[str]:
    """Parse the ``./hephaestus/...`` tokens from the justfile ``docs`` recipe."""
    lines = _read("justfile").splitlines()
    recipe = next(
        (index for index, line in enumerate(lines) if line.rstrip() == "docs:"),
        None,
    )
    assert recipe is not None, "justfile lost the docs recipe"
    tokens: set[str] = set()
    for line in lines[recipe + 1 :]:
        if not line.startswith((" ", "\t")):
            break
        tokens.update(part for part in line.split() if part.startswith("./hephaestus"))
    return tokens


def test_pdoc_targets_match_subpackages() -> None:
    """The justfile docs recipe and expected_pdoc_targets agree in both directions."""
    documented = _justfile_docs_pdoc_tokens()
    expected = set(expected_pdoc_targets(REPO_ROOT))
    assert documented == expected, (
        f"justfile docs recipe does not match expected_pdoc_targets: "
        f"missing_from_recipe={sorted(expected - documented)}, "
        f"not_expected={sorted(documented - expected)}"
    )


def _prose_paragraphs(repo_relative: str) -> list[str]:
    """Return the non-fenced paragraph text of one Markdown document.

    Consecutive non-blank, non-fenced lines join into one paragraph, so a
    sentence that wraps across lines still reads as one sentence.
    """
    fenced = False
    paragraphs: list[str] = []
    current: list[str] = []
    for line in _read(repo_relative).splitlines():
        if line.strip().startswith("```"):
            fenced = not fenced
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        if fenced:
            continue
        if not line.strip():
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        current.append(line.strip())
    if current:
        paragraphs.append(" ".join(current))
    return paragraphs


def test_future_path_prose_names_owning_issue() -> None:
    """Prose that names a not-yet-existing path also names its owning issue."""
    documents = [
        "AGENTS.md",
        *(f"docs/{path.name}" for path in (REPO_ROOT / "docs").glob("*.md")),
    ]
    violations: list[str] = []
    for document in documents:
        for paragraph in _prose_paragraphs(document):
            link_spans = [
                (match.start(1), match.end(1)) for match in _LINK_TARGET.finditer(paragraph)
            ]
            sentences = _line_sentences(paragraph)
            for match in _FUTURE_PATH.finditer(paragraph):
                name = match.group(1)
                if (REPO_ROOT / "hephaestus" / name).is_dir():
                    continue
                if any(start <= match.start() < end for start, end in link_spans):
                    violations.append(f"{document}: {name} appears inside a Markdown link target")
                    continue
                sentence = next(
                    (text for start, end, text in sentences if start <= match.start() < end),
                    paragraph,
                )
                if re.search(r"#\d+", sentence) is None:
                    violations.append(f"{document}: {name} appears without an owning issue")
    assert not violations, f"future-path prose violations: {violations}"


def _line_sentences(line: str) -> list[tuple[int, int, str]]:
    """Split one line into (start, end, text) sentence spans."""
    spans: list[tuple[int, int, str]] = []
    start = 0
    for breaker in _SENTENCE_BREAK.finditer(line):
        spans.append((start, breaker.start(), line[start : breaker.start()]))
        start = breaker.end()
    spans.append((start, len(line), line[start:]))
    return spans


def test_architecture_section_11_future_path_paragraph_is_below_facade_table() -> None:
    """The section-11 Git-family paragraph sits between the facade table and section 12."""
    lines = _read("docs/architecture.md").splitlines()
    section_11 = next(
        (index for index, line in enumerate(lines) if line.startswith("## 11. ")),
        None,
    )
    facade_table = next(
        (index for index, line in enumerate(lines) if line.startswith("| Facade |")),
        None,
    )
    section_12 = next(
        (index for index, line in enumerate(lines) if line.startswith("## 12. ")),
        None,
    )
    assert section_11 is not None, "docs/architecture.md lost the section-11 heading"
    assert facade_table is not None, "docs/architecture.md lost the section-11 facade table"
    assert section_12 is not None, "docs/architecture.md lost the section-12 heading"
    paragraph = next(
        (
            index
            for index in range(facade_table + 1, section_12)
            if "hephaestus/git/" in lines[index]
        ),
        None,
    )
    assert paragraph is not None, (
        "docs/architecture.md section 11 has no paragraph naming hephaestus/git/"
    )
    assert section_11 < facade_table < paragraph < section_12, (
        "the section-11 Git-family paragraph must sit below the facade table "
        "and above the section-12 heading"
    )
