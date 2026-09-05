"""Match GitHub ruleset refs with Ruby ``File::FNM_PATHNAME`` semantics."""

from __future__ import annotations

import re


def _character_class(pattern: str, start: int) -> tuple[str, int]:
    """Translate one shell character class, or one unmatched opening bracket."""
    end = start + 1
    if end < len(pattern) and pattern[end] in {"!", "^"}:
        end += 1
    if end < len(pattern) and pattern[end] == "]":
        end += 1
    while end < len(pattern) and pattern[end] != "]":
        end += 1
    if end == len(pattern):
        return r"\[", start + 1
    content = pattern[start + 1 : end]
    if content.startswith("!"):
        content = "^" + content[1:]
    elif content.startswith("^"):
        content = "\\" + content
    content = content.replace("\\", r"\\")
    return rf"(?!/)[{content}]", end + 1


def _pathname_regex(pattern: str) -> str:
    """Translate one GitHub ruleset pattern to an anchored regular expression."""
    translated: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index) and (index == 0 or pattern[index - 1] == "/"):
            translated.append(r"(?:[^/]+/)*")
            index += 3
            continue
        character = pattern[index]
        if character == "*":
            while index < len(pattern) and pattern[index] == "*":
                index += 1
            translated.append(r"[^/]*")
            continue
        if character == "?":
            translated.append(r"[^/]")
        elif character == "[":
            character_class, index = _character_class(pattern, index)
            translated.append(character_class)
            continue
        elif character == "\\" and index + 1 < len(pattern):
            index += 1
            translated.append(re.escape(pattern[index]))
        else:
            translated.append(re.escape(character))
        index += 1
    return "".join(translated)


def pathname_pattern_matches(value: str, pattern: str) -> bool:
    """Return whether a ref matches GitHub's path-separator-aware pattern."""
    return re.fullmatch(_pathname_regex(pattern), value) is not None
