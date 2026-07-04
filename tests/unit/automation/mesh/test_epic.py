"""Tests for hephaestus.automation.mesh.epic."""

from __future__ import annotations

from hephaestus.automation.mesh.epic import (
    EpicChild,
    epic_key,
    parse_task_list,
    render_task_list,
)

EPIC_BODY = """# Epic: do the thing

Some prose describing the epic.

- [ ] #123 (depends on: #456)
- [x] #124
- [ ] #125 (depends on: #123, #124)

More prose after the list.
"""


class TestParseTaskList:
    """Tests for parse_task_list()."""

    def test_parses_children_and_deps(self) -> None:
        children = parse_task_list(EPIC_BODY)
        assert [c.number for c in children] == [123, 124, 125]
        assert children[0].depends_on == [456]
        assert children[1].checked is True
        assert children[2].depends_on == [123, 124]

    def test_ignores_prose(self) -> None:
        assert parse_task_list("no list here\n- regular bullet #7\n") == []

    def test_empty_body(self) -> None:
        assert parse_task_list("") == []


class TestRenderTaskList:
    """Tests for render_task_list()."""

    def test_round_trip(self) -> None:
        children = parse_task_list(EPIC_BODY)
        rendered = render_task_list(children)
        assert parse_task_list(rendered) == children

    def test_renders_deps(self) -> None:
        line = render_task_list([EpicChild(number=9, depends_on=[1, 2])])
        assert line == "- [ ] #9 (depends on: #1, #2)"

    def test_renders_checked(self) -> None:
        assert render_task_list([EpicChild(number=3, checked=True)]) == "- [x] #3"


class TestEpicKey:
    """Tests for epic_key()."""

    def test_slugifies_repo(self) -> None:
        assert epic_key("HomericIntelligence/Odysseus", 42) == ("homericintelligence-odysseus-42")
