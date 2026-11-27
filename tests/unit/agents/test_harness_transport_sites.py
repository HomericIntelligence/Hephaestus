"""Golden command tests for the catalog-driven effort sites."""

from __future__ import annotations

from pathlib import Path

import pytest

from hephaestus.agents import runtime
from hephaestus.agents.execution_policy import SessionLifecycle
from hephaestus.agents.model_selection import AgentModelSelection


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("m:max", ["--model", "m", "-c", 'model_reasoning_effort="max"']),
        ("m:default", ["--model", "m"]),
        ("m", ["--model", "m"]),
        ("", []),
    ],
)
def test_codex_model_args_keep_golden_form(model: str, expected: list[str]) -> None:
    """The Codex site carries the same argv after the transport change."""
    assert runtime._codex_model_args(model) == expected


def test_codex_resume_keeps_the_selected_effort() -> None:
    """An effort is not dropped when a Codex session resumes."""
    cmd = runtime._codex_base_cmd(cwd=Path("."), model="m:max", resume_id="session-1")
    assert cmd[:5] == ["codex", "exec", "resume", "session-1", "--model"]
    assert 'model_reasoning_effort="max"' in cmd


@pytest.mark.parametrize(
    ("model", "expected_tail"),
    [
        ("p/m:high", ["--model", "p/m", "--variant", "high"]),
        ("p/m:default", ["--model", "p/m"]),
    ],
)
def test_opencode_variant_keeps_golden_form(model: str, expected_tail: list[str]) -> None:
    """OpenCode V1 keeps its separate effort flag."""
    cmd = runtime._opencode_base_cmd(cwd=Path("."), model=model)
    assert cmd[:6] == ["opencode", "run", "--dir", ".", "--format", "json"]
    model_index = cmd.index("--model")
    assert cmd[model_index : model_index + len(expected_tail)] == expected_tail


@pytest.mark.parametrize(
    ("model", "expected_model"),
    [
        ("p/m:high", "p/m#high"),
        ("p/m:default", "p/m"),
        ("p/m", "p/m"),
    ],
)
def test_opencode2_inline_keeps_golden_form(model: str, expected_model: str) -> None:
    """OpenCode 2 keeps the effort inside the model reference."""
    cmd = runtime._opencode2_base_cmd(model=model)
    assert cmd[cmd.index("--model") + 1] == expected_model
    assert "--variant" not in cmd


@pytest.mark.parametrize(
    ("model", "thinking", "expected_thinking"),
    [
        ("m:high", "", "high"),
        ("m:high", "medium", "medium"),
        ("m:default", "", None),
    ],
)
def test_pi_thinking_keeps_golden_form(
    model: str, thinking: str, expected_thinking: str | None
) -> None:
    """Pi keeps its thinking flag after the selected model."""
    cmd = runtime._pi_automation_cmd(
        Path("/tmp/pi"), model=model, thinking=thinking, lifecycle=SessionLifecycle.ONE_SHOT
    )
    assert cmd[cmd.index("--model") + 1] == "m"
    if expected_thinking is None:
        assert "--thinking" not in cmd
    else:
        assert cmd[cmd.index("--thinking") + 1] == expected_thinking


def test_shared_codex_effort_retry_uses_default_sentinel() -> None:
    """The shared worker helper retries an explicit effort with the default."""
    attempts: list[str] = []

    def execute(reference: str) -> runtime.AgentRunResult:
        attempts.append(reference)
        if len(attempts) == 1:
            raise runtime._CodexReasoningEffortRejectedError("unsupported")
        return runtime.AgentRunResult(stdout="ok", stderr="")

    result = runtime.run_codex_effort_attempts(execute, "m:max")
    assert result.stdout == "ok"
    assert attempts == ["m:max", AgentModelSelection("m", "default").reference]


def test_shared_codex_effort_retry_does_not_repeat_default() -> None:
    """A rejected default effort propagates without a second attempt."""
    attempts: list[str] = []

    def execute(reference: str) -> runtime.AgentRunResult:
        attempts.append(reference)
        raise runtime._CodexReasoningEffortRejectedError("unsupported")

    with pytest.raises(runtime._CodexReasoningEffortRejectedError):
        runtime.run_codex_effort_attempts(execute, "m:default")
    assert attempts == ["m:default"]
