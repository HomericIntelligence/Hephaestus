"""Open-issue duplicate detection for follow-up issue filing.

Used by :mod:`hephaestus.automation.follow_up` to avoid re-filing near-duplicate
issues across parallel planning runs. Provides:

- :func:`find_duplicate_open_issue` — searches open issues by title keywords
  and returns the best match scoring above a configurable threshold.
- :func:`extract_new_info` — paragraph-level set diff used to decide whether a
  duplicate-detected follow-up adds new context worth posting as a comment.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from .github_api import _gh_call

logger = logging.getLogger(__name__)

# Words that don't help disambiguate issue titles when used as search keywords.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "if",
        "in",
        "on",
        "to",
        "of",
        "for",
        "with",
        "is",
        "are",
        "was",
        "were",
        "be",
        "by",
        "as",
        "at",
        "from",
        "this",
        "that",
        "it",
        "its",
        "fix",
        "add",
        "update",
        "remove",
        "use",
        "make",
        "new",
        "issue",
        "bug",
        "feature",
        "task",
    }
)

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]+")

DEFAULT_TITLE_THRESHOLD = 0.85
DEFAULT_PARAGRAPH_NEW_THRESHOLD = 0.6

# Titles with fewer than this many content tokens after stopword removal fall
# back to character tri-gram similarity, which is more reliable for short strings
# like "Fix typo" or "Add index".
_SHORT_TITLE_TOKEN_THRESHOLD = 3


@dataclass(frozen=True)
class IssueMatch:
    """A candidate duplicate issue."""

    number: int
    title: str
    body: str
    similarity: float


def _tokens(text: str) -> set[str]:
    """Lowercase content tokens with stopwords removed."""
    return {
        t.lower()
        for t in _TOKEN_RE.findall(text or "")
        if t.lower() not in _STOPWORDS and len(t) > 2
    }


def _jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity of two token sets."""
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _trigrams(text: str) -> set[str]:
    """Return the set of character tri-grams from *text* (lowercased, spaces collapsed)."""
    normalized = re.sub(r"\s+", " ", text.lower().strip())
    if len(normalized) < 3:
        return {normalized} if normalized else set()
    return {normalized[i : i + 3] for i in range(len(normalized) - 2)}


def _trigram_similarity(a: str, b: str) -> float:
    """Jaccard similarity over character tri-grams of *a* and *b*.

    Used as a fallback similarity measure for short titles that don't have
    enough content tokens for reliable Jaccard-on-words scoring.
    """
    tg_a = _trigrams(a)
    tg_b = _trigrams(b)
    return _jaccard(tg_a, tg_b)


def _title_similarity(a: str, b: str) -> float:
    """Compute the best available similarity score between two issue titles.

    For titles with at least ``_SHORT_TITLE_TOKEN_THRESHOLD`` content tokens,
    uses token-level Jaccard (fast, high precision).  For shorter titles (e.g.
    "Fix typo", "Add index") it falls back to character tri-gram Jaccard, which
    handles short strings far better than the word-level metric.

    Args:
        a: First issue title.
        b: Second issue title.

    Returns:
        Similarity score in [0, 1].

    """
    tokens_a = _tokens(a)
    tokens_b = _tokens(b)
    if (
        len(tokens_a) >= _SHORT_TITLE_TOKEN_THRESHOLD
        and len(tokens_b) >= _SHORT_TITLE_TOKEN_THRESHOLD
    ):
        return _jaccard(tokens_a, tokens_b)
    # Short-title fallback: tri-gram similarity
    return _trigram_similarity(a, b)


def _search_keywords(title: str, max_terms: int = 3) -> str:
    """Pick up to ``max_terms`` distinctive tokens from a title for ``gh issue list --search``.

    Prefers longer tokens (more distinctive) over short ones.
    """
    tokens = sorted(_tokens(title), key=len, reverse=True)
    return " ".join(tokens[:max_terms])


def find_duplicate_open_issue(
    title: str,
    body: str = "",
    *,
    threshold: float = DEFAULT_TITLE_THRESHOLD,
    search_limit: int = 20,
) -> IssueMatch | None:
    """Find an open issue whose title is a near-duplicate of *title*.

    Args:
        title: Candidate issue title to dedup against.
        body: Candidate issue body (used only when returning the match — the
            similarity score is computed from titles, which are the strongest
            duplicate signal in practice).
        threshold: Minimum Jaccard similarity over title tokens to count as a
            duplicate. Default 0.85 — strict enough to avoid false positives.
        search_limit: Cap on issues fetched from ``gh issue list --search``.

    Returns:
        Best-matching :class:`IssueMatch` at or above *threshold*, else ``None``.

    """
    keywords = _search_keywords(title)
    if not keywords:
        # Title has no distinctive tokens; can't search safely.
        return None

    try:
        result = _gh_call(
            [
                "issue",
                "list",
                "--state",
                "open",
                "--search",
                keywords,
                "--limit",
                str(search_limit),
                "--json",
                "number,title,body",
            ],
        )
        candidates: list[dict[str, Any]] = json.loads(result.stdout or "[]")
    except Exception as e:
        # Search is best-effort; on failure, behave as if no duplicate exists
        # so the caller files the issue normally.
        logger.warning(f"Duplicate search failed for '{title[:60]}': {e}")
        return None

    best: IssueMatch | None = None
    for c in candidates:
        c_title = c.get("title", "") or ""
        score = _title_similarity(title, c_title)
        if score >= threshold and (best is None or score > best.similarity):
            best = IssueMatch(
                number=int(c["number"]),
                title=c_title,
                body=c.get("body", "") or "",
                similarity=score,
            )

    if best:
        logger.info(
            f"Duplicate detected: '{title[:60]}' ~ #{best.number} "
            f"'{best.title[:60]}' (score={best.similarity:.2f})"
        )
    return best


def _paragraphs(text: str) -> list[str]:
    """Split text into non-empty paragraphs (separated by blank lines)."""
    return [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]


def extract_new_info(
    new_body: str,
    existing_body: str,
    *,
    new_threshold: float = DEFAULT_PARAGRAPH_NEW_THRESHOLD,
) -> str:
    """Return paragraphs of *new_body* that are not already represented in *existing_body*.

    A paragraph is considered "already represented" if its token-Jaccard with
    any existing paragraph is at or above *new_threshold*. The default 0.6 is
    looser than the title threshold because paragraphs are longer and have more
    incidental overlap.

    Args:
        new_body: Body of the would-be new issue.
        existing_body: Body of the existing duplicate issue.
        new_threshold: Paragraph-similarity threshold above which a paragraph is
            considered a restatement and dropped.

    Returns:
        Concatenation of novel paragraphs, joined by blank lines. Empty string
        if every paragraph in *new_body* restates something in *existing_body*.

    """
    new_paras = _paragraphs(new_body)
    existing_paras = _paragraphs(existing_body)
    existing_token_sets = [_tokens(p) for p in existing_paras]

    novel: list[str] = []
    for para in new_paras:
        para_tokens = _tokens(para)
        if not para_tokens:
            continue
        # Drop if any existing paragraph already covers it.
        if any(_jaccard(para_tokens, ex) >= new_threshold for ex in existing_token_sets):
            continue
        novel.append(para)

    return "\n\n".join(novel)
