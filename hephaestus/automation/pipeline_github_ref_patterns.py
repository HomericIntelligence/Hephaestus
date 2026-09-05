"""Match GitHub ruleset refs with Ruby ``File::FNM_PATHNAME`` semantics."""

from __future__ import annotations

import re


def _class_characters(content: str) -> list[tuple[str, bool]]:
    """Return character-class members with their escape state."""
    characters: list[tuple[str, bool]] = []
    index = 0
    while index < len(content):
        if content[index] != "\\":
            characters.append((content[index], False))
            index += 1
            continue
        if index + 1 == len(content):
            raise ValueError("ruleset ref character class has an incomplete escape")
        characters.append((content[index + 1], True))
        index += 2
    return characters


def _translate_class_characters(characters: list[tuple[str, bool]]) -> str:
    """Translate validated Ruby class members to regular-expression members."""
    translated: list[str] = []
    last = len(characters) - 1
    for position, (character, escaped) in enumerate(characters):
        if character == "-":
            translated.append("-" if not escaped and 0 < position < last else r"\-")
        elif character in {"\\", "]", "^"}:
            translated.append("\\" + character)
        else:
            translated.append(re.escape(character))
    return "".join(translated)


def _character_class(pattern: str, start: int) -> tuple[str, int]:
    """Translate one Ruby shell character class or reject invalid syntax."""
    end = start + 1
    while end < len(pattern):
        if pattern[end] == "\\" and end + 1 < len(pattern):
            end += 2
            continue
        if pattern[end] == "]":
            break
        end += 1
    if end == len(pattern):
        raise ValueError("ruleset ref pattern has an unterminated character class")
    content = pattern[start + 1 : end]
    if not content:
        raise ValueError("ruleset ref pattern has an empty character class")
    if content.startswith("[:") and content.endswith(":"):
        raise ValueError("ruleset ref pattern has an unsupported POSIX character class")
    negated = content[0] in {"!", "^"}
    if negated:
        content = content[1:]
    if not content:
        raise ValueError("ruleset ref pattern has an empty character class")

    characters = _class_characters(content)
    translated = _translate_class_characters(characters)
    return rf"(?!/)[{'^' if negated else ''}{translated}]", end + 1


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
    try:
        return re.fullmatch(_pathname_regex(pattern), value) is not None
    except re.error as exc:
        raise ValueError("ruleset ref pattern has an invalid character class") from exc
