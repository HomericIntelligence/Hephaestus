"""Guard against re-introducing emoji in scripts/compare_benchmarks.py stderr output."""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "compare_benchmarks.py"

# UTF-8 byte sequences covering the Misc Symbols / Pictographs ranges
# used by ❌ (U+274C → \xe2\x9d\x8c) and ✅ (U+2705 → \xe2\x9c\x85), which are
# 3-byte (BMP) encodings, plus the 4-byte supplementary-plane lead
# (\xf0\x9f...) used by the wider emoji planes in the Markdown report.
EMOJI_BYTE_PREFIXES = (b"\xf0\x9f", b"\xe2\x9d\x8c", b"\xe2\x9c\x85")


def _write_results(path: Path, mean_ns: float) -> None:
    path.write_text(
        json.dumps(
            {
                "benchmarks": [
                    {"name": "bench_a", "duration_ms": mean_ns},
                ],
            }
        )
    )


def _run(current: Path, baseline: Path) -> subprocess.CompletedProcess[bytes]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(current), str(baseline)],
        capture_output=True,
        check=False,
        env=env,
    )


def test_pass_path_emits_no_emoji_on_stderr(tmp_path: Path) -> None:
    """Verify no emoji bytes appear in stderr when no critical regressions found."""
    current = tmp_path / "current.json"
    baseline = tmp_path / "baseline.json"
    _write_results(current, 1.0)
    _write_results(baseline, 1.0)

    result = _run(current, baseline)

    assert result.returncode == 0
    for prefix in EMOJI_BYTE_PREFIXES:
        assert prefix not in result.stderr, f"emoji prefix {prefix!r} leaked into stderr"
    assert b"PASS" in result.stderr


def test_fail_path_emits_no_emoji_on_stderr(tmp_path: Path) -> None:
    """Verify no emoji bytes appear in stderr when critical regressions detected."""
    current = tmp_path / "current.json"
    baseline = tmp_path / "baseline.json"
    # >25% slower → critical regression → fail path
    _write_results(current, 2.0)
    _write_results(baseline, 1.0)

    result = _run(current, baseline)

    assert result.returncode == 1
    for prefix in EMOJI_BYTE_PREFIXES:
        assert prefix not in result.stderr, f"emoji prefix {prefix!r} leaked into stderr"
    assert b"FAIL" in result.stderr
