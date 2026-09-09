"""Select a bounded skill corpus for advice and learning."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from hephaestus.automation.athena_contract import AthenaContractReceipt
from hephaestus.automation.mnemosyne_binding import MnemosyneBindingReceipt
from hephaestus.automation.mnemosyne_corpus import (
    MnemosyneCorpusError,
    MnemosyneCorpusResult,
    SkillSelection,
    read_selected_skill_corpus,
)
from hephaestus.automation.pipeline.athena_skill_jobs import AthenaSkillRequest
from hephaestus.config.child_environments import build_git_child_env
from hephaestus.utils.helpers import run_subprocess


class DefaultCorpusReader:
    """Select and read a bounded Athena-compatible skill corpus.

    Pipeline requests carry issue context, rather than model-chosen filenames.
    The host therefore performs deterministic retrieval from the already-bound
    commit.  Explicit selections remain available only to closed callers that
    already have validated selection evidence.
    """

    _MAX_SELECTED_SKILLS = 5
    _TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_-]{2,}")
    _STOP_WORDS = frozenset(
        {
            "about",
            "after",
            "against",
            "also",
            "and",
            "are",
            "been",
            "before",
            "being",
            "between",
            "but",
            "can",
            "code",
            "for",
            "from",
            "has",
            "have",
            "into",
            "issue",
            "its",
            "not",
            "only",
            "our",
            "should",
            "that",
            "the",
            "their",
            "then",
            "this",
            "through",
            "with",
            "will",
            "would",
            "you",
            "your",
        }
    )

    def __init__(
        self,
        *,
        git_output: Callable[[Path, tuple[str, ...]], str] | None = None,
    ) -> None:
        """Initialize retrieval with an injectable committed-object reader."""
        self._git_output = git_output or self._subprocess_git_output

    @staticmethod
    def _subprocess_git_output(root: Path, argv: tuple[str, ...]) -> str:
        """Read one committed Git object while preserving fail-closed errors."""
        try:
            result = run_subprocess(
                ["git", *argv],
                cwd=root,
                check=False,
                env=build_git_child_env(),
                track_process_group=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise MnemosyneCorpusError(f"Mnemosyne corpus search failed: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise MnemosyneCorpusError(
                f"Mnemosyne corpus search failed for {' '.join(argv)}: "
                f"{detail or result.returncode}"
            )
        return result.stdout or ""

    @classmethod
    def _is_flat_skill_path(cls, source: str) -> bool:
        """Return whether ``source`` is a contract-admitted flat skill path."""
        path = PurePosixPath(source)
        return bool(
            not path.is_absolute()
            and ".." not in path.parts
            and len(path.parts) == 2
            and path.parts[0] == "skills"
            and path.suffix == ".md"
            and not path.name.endswith(".notes.md")
        )

    @classmethod
    def _query_terms(cls, request: AthenaSkillRequest) -> tuple[str, ...]:
        """Extract semantic retrieval terms from the ordinary pipeline payload."""
        fields = (
            request.payload.get("issue_title", ""),
            request.payload.get("issue_body", ""),
            request.payload.get("context", ""),
        )
        terms = {
            token
            for value in fields
            if isinstance(value, str)
            for token in cls._TOKEN_RE.findall(value.casefold())
            if token not in cls._STOP_WORDS
        }
        return tuple(sorted(terms))

    @classmethod
    def _score_skill(cls, content: str, source: str, terms: tuple[str, ...]) -> tuple[int, str]:
        """Rank semantic outcome/constraint/failure matches before title wording."""
        lowered = content.casefold()
        source_name = Path(source).stem.casefold()
        matched: list[str] = []
        score = 0
        for term in terms:
            occurrences = lowered.count(term)
            if not occurrences:
                continue
            matched.append(term)
            # The whole committed entry is searched, including frontmatter
            # description/category/tags and trigger/result history.  Stronger
            # signals deliberately favor outcome, constraint, and failure
            # guidance over a coincidental filename match.
            score += min(occurrences, 3)
            if re.search(
                rf"(?im)^#+\s*.*(?:outcome|goal|desired|constraint|failure|failed|result|trigger).*{re.escape(term)}",
                content,
            ):
                score += 8
            if re.search(
                rf"(?im)^(?:description|category|tags|trigger|failure|result):.*{re.escape(term)}",
                content,
            ):
                score += 4
            if term in source_name:
                score += 1
        return score, ", ".join(matched[:4])

    def _explicit_selections(self, raw: object) -> tuple[SkillSelection, ...]:
        """Validate closed caller-provided selections instead of silently dropping them."""
        if not isinstance(raw, (list, tuple)):
            raise MnemosyneCorpusError("selected_skills must be a list of selection objects")
        selections: list[SkillSelection] = []
        for item in raw:
            if not isinstance(item, dict):
                raise MnemosyneCorpusError("selected_skills contains a non-object selection")
            name, source, reason = item.get("name"), item.get("source"), item.get("reason")
            if (
                not isinstance(name, str)
                or not isinstance(source, str)
                or not isinstance(reason, str)
            ):
                raise MnemosyneCorpusError(
                    "selected_skills entries require string name, source, and reason"
                )
            selections.append(SkillSelection(name=name, source=source, reason=reason))
        return tuple(selections)

    def _select_from_bound_corpus(
        self,
        request: AthenaSkillRequest,
        binding: MnemosyneBindingReceipt,
    ) -> tuple[SkillSelection, ...]:
        """Search and rank flat skills from the immutable bound commit."""
        terms = self._query_terms(request)
        if not terms:
            return ()
        paths = self._git_output(
            Path(binding.root),
            ("ls-tree", "-r", "--name-only", binding.commit_sha, "--", "skills"),
        ).splitlines()
        ranked: list[tuple[int, str, str, str]] = []
        for source in paths:
            if not self._is_flat_skill_path(source):
                continue
            content = self._git_output(
                Path(binding.root), ("show", f"{binding.commit_sha}:{source}")
            )
            score, matches = self._score_skill(content, source, terms)
            if score:
                ranked.append((score, source, Path(source).stem, matches))
        ranked.sort(key=lambda entry: (-entry[0], entry[1]))
        return tuple(
            SkillSelection(
                name=name,
                source=source,
                reason=f"Matched intended outcome, constraint, or failure terms: {matches}",
            )
            for _score, source, name, matches in ranked[: self._MAX_SELECTED_SKILLS]
        )

    def read(
        self,
        request: AthenaSkillRequest,
        binding: MnemosyneBindingReceipt,
        contract: AthenaContractReceipt,
    ) -> MnemosyneCorpusResult:
        """Read supplied selections or retrieve them from the bound corpus."""
        raw_selections = request.payload.get("selected_skills")
        selections = (
            self._explicit_selections(raw_selections)
            if raw_selections is not None
            else self._select_from_bound_corpus(request, binding)
        )
        return read_selected_skill_corpus(
            root=Path(binding.root),
            binding=binding,
            contract=contract,
            selections=selections,
        )
