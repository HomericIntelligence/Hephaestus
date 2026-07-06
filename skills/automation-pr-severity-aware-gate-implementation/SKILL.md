---
name: automation-pr-severity-aware-gate-implementation
category: architecture
description: Severity-aware PR review GO gate that filters blocking vs advisory automation threads using HTML comment markers in review bodies
argument-hint: —
trigger-terms:
  - severity-aware automation thread filtering
  - advisory-only thread deadlock prevention
  - automation thread gate severity classification
  - REST API metadata persistence patterns in automation gates
  - HTML comment marker persistence in GitHub API payloads
sources:
  - hephaestus#1856 (fix: filter GO gate by thread severity)
  - hephaestus#1554 (re-introduced minor-thread deadlock)
version: 1.0.0
---

# Severity-Aware Automation Gate Implementation

## Problem

The PR review GO verdict was incorrectly downgraded to NOGO whenever ANY unresolved automation thread remained, even if the reviewer classified it as a **minor** or **nitpick** (advisory) comment. This deadlocked PRs because:

1. The reviewer returns a GO verdict with the PR accepted
2. The automation thread for the reviewer's own advisory nitpick remains unresolved
3. The GO gate checks `unresolved_threads > 0` and downgrades to NOGO
4. This triggers another review round, starting an infinite loop
5. PR ends up in `state:skip` (issue #1554 minor-thread deadlock)

## Root Cause

The gate logic was binary: GO if `unresolved_threads == 0`, NOGO otherwise. It did not distinguish:

- **Blocking** threads (critical/major severity): should block a GO
- **Advisory** threads (minor/nitpick severity): reviewer already waved these; should not re-block

## Solution

Split unresolved automation threads into blocking vs advisory using severity markers embedded in the thread body at post-time.

### 1. Severity Marker Embedding

When posting a review comment, prepend an HTML marker line to the comment body:

```python
def _with_severity_marker(comment: dict[str, Any]) -> str:
    """Prepend the <!-- hephaestus-severity: X --> marker line (#1856)."""
    sev = str(comment.get("severity") or "").strip().lower()
    if sev not in VALID_SEVERITIES:
        sev = "major"  # Fail-safe: unknown = blocking
    body = str(comment.get("body") or "")
    if body.lstrip().startswith(SEVERITY_MARKER_PREFIX):
        return body  # Idempotent: already marked
    return f"{SEVERITY_MARKER_PREFIX} {sev} -->\n{body}"
```

**Key design points:**

- **Fail-safe default**: unmarked or unknown severity is treated as `major` (blocking)
- **Idempotent**: if marker already exists (e.g., re-posting after validation), don't double-prepend
- **Preserved through round-trip**: GitHub API stores and returns the full comment body including the marker

### 2. Severity Constants

Define in `hephaestus/automation/prompts/pr_review.py`:

```python
BLOCKING_SEVERITIES: frozenset[str] = frozenset({"critical", "major"})
VALID_SEVERITIES: frozenset[str] = frozenset({"critical", "major", "minor", "nitpick"})
SEVERITY_MARKER_PREFIX = "<!-- hephaestus-severity:"
```

**Severity semantics:**

- `critical`: correctness/security bug or data loss — always blocks
- `major`: design/maintainability problem — always blocks
- `minor`: small genuine improvement (naming, edge case) — advisory only
- `nitpick`: cosmetic/stylistic preference — purely advisory

### 3. Severity Extraction and Classification

Extract severity from the marker using line-prefix anchoring (avoids false positives):

```python
def _thread_severity_is_blocking(thread: dict[str, Any]) -> bool:
    """Return True if the thread's recovered severity is blocking."""
    body = str(thread.get("body") or "")
    for line in body.splitlines():
        stripped = line.strip()
        # Match full marker line exactly: line-prefix anchored
        if stripped.startswith(SEVERITY_MARKER_PREFIX) and stripped.endswith("-->"):
            sev = stripped[len(SEVERITY_MARKER_PREFIX):-3].strip().lower()
            return sev in BLOCKING_SEVERITIES
    return True  # Missing marker = blocking (fail-safe)
```

**Critical detail:** Line-prefix anchoring prevents substring false positives. A comment that happens to say "<!-- hephaestus-severity: minor -->" mid-sentence is correctly ignored — only markers on their own lines are parsed.

### 4. Three-Tuple Thread Count by Severity

Return `(blocking_automation, minor_automation, human)` from a new helper:

```python
def count_unresolved_threads_by_severity(self, pr_number: int) -> tuple[int, int, int]:
    """Return (blocking_automation, minor_automation, human) (#1856).

    Severity is read from the <!-- hephaestus-severity: X --> marker
    prepended at post time; a missing/garbled marker counts as BLOCKING
    (fail-safe).
    """
    threads = self._unresolved_threads(pr_number)
    if not threads:
        return (0, 0, 0)
    current_login = github_api.gh_current_login()
    blocking = minor = human = 0
    for t in threads:
        if _is_automation_owned_thread(t, current_login):
            if _thread_severity_is_blocking(t):
                blocking += 1
            else:
                minor += 1
        else:
            human += 1
    return (blocking, minor, human)
```

**Return tuple semantics:**

- `blocking_automation`: count of automation threads marked critical/major (or unmarked/unparseable)
- `minor_automation`: count of automation threads marked minor/nitpick
- `human`: count of human reviewer threads (always blocking — automation never downgrades human feedback)

### 5. Severity-Aware GO Gate

The GO gate now checks only `blocking_auto == 0`:

```python
# In pr_review.py:703
blocking_auto, minor_auto, human_unresolved = (
    ctx.github.count_unresolved_threads_by_severity(item.pr)
)

if verdict.is_go and human_unresolved:
    # Human threads always block (unchanged)
    return StageOutcome(Disposition.FINISH_FAIL, "human_blocked")

if verdict.is_go and blocking_auto == 0:
    # Clean GO: zero blocking threads, minor threads OK
    if minor_auto:
        # Resolve advisory minor/nitpick threads so
        # required_review_thread_resolution does not re-block at merge
        ctx.github.resolve_automation_threads(item.pr)
    self._write_go_and_arm(item.pr, ctx)
    return ...
```

**Key invariant:** If the reviewer returned a GO, then:

- Any advisory (minor/nitpick) threads are threads they explicitly waved
- We resolve those advisory threads automatically before arming
- So `required_review_thread_resolution` at the merge gate does not re-block

### 6. Advisory Thread Resolution Before Arming

A new helper automatically resolves automation-owned advisory threads:

```python
def resolve_automation_threads(self, pr_number: int) -> int:
    """Resolve unresolved AUTOMATION-owned threads; return the count (#1856).

    Never resolves human threads. Used by the GO gate to clear advisory
    minor/nitpick threads the reviewer waved so required_review_thread_
    resolution does not re-block the armed PR at merge.
    """
    if self._skip(f"resolve automation threads on PR #{pr_number}"):
        return 0
    threads = self._unresolved_threads(pr_number)
    current_login = github_api.gh_current_login()
    resolved = 0
    for t in threads:
        if _is_automation_owned_thread(t, current_login) and t.get("id"):
            github_api.gh_pr_resolve_thread(str(t["id"]), dry_run=self.dry_run)
            resolved += 1
    return resolved
```

**Design choice:** Resolve *all* unresolved automation threads, not just minor ones. This is correct because:

1. If the reviewer returned a GO with only blocking threads open, the gate doesn't arm (previous check: `blocking_auto == 0` failed)
2. If we reach this code, blocking_auto == 0, so all remaining automation threads are advisory
3. Resolving them is safe and prevents merge-stage re-block

## Files Modified

- `hephaestus/automation/prompts/pr_review.py` — severity constants and marker definition
- `hephaestus/automation/pipeline_github.py` — marker embedding, severity extraction, 3-tuple counter, thread resolver
- `hephaestus/automation/pipeline/stages/pr_review.py` — GO gate logic using severity count
- `hephaestus/automation/pipeline/stages/base.py` — StageGitHub protocol docstring clarification
- `tests/unit/automation/pipeline/stages/conftest.py` — test fixtures for marker/severity testing
- `tests/unit/automation/pipeline/stages/test_stage_pr_review.py` — 96 new lines, ~12 new marker/severity tests
- `tests/unit/automation/test_pipeline_github.py` — 142 new lines, marker/severity extraction unit tests

## Test Coverage

- **75 existing tests pass** (no regressions in marker embedding, gate logic, thread counting)
- **12 new tests** for marker embedding, severity extraction, 3-tuple counting, advisory resolution
- **Coverage**: 100% of marker/severity code paths exercised

### Example Tests

```python
def test_thread_severity_is_blocking_critical():
    """Critical severity is blocking."""
    thread = {"body": "<!-- hephaestus-severity: critical -->\nComment"}
    assert _thread_severity_is_blocking(thread) is True

def test_thread_severity_is_blocking_minor():
    """Minor severity is not blocking."""
    thread = {"body": "<!-- hephaestus-severity: minor -->\nComment"}
    assert _thread_severity_is_blocking(thread) is False

def test_thread_severity_missing_is_blocking():
    """Missing severity marker defaults to blocking (fail-safe)."""
    thread = {"body": "Just a comment, no marker"}
    assert _thread_severity_is_blocking(thread) is True
```

## Failure Modes & Mitigations

### 1. Marker Corruption in GitHub API

**Risk**: Marker stripped or mangled during GitHub round-trip.

**Mitigation**: Fail-safe default. If the marker is missing or unparseable, severity defaults to `major` (blocking). This reproduces pre-#1856 all-blocking behavior until the marker is seeded, preventing silent downgrades.

### 2. Substring False Positives

**Risk**: A comment that happens to mention "hephaestus-severity" is misparsed.

**Mitigation**: Line-prefix anchoring. Only lines where `stripped.startswith(SEVERITY_MARKER_PREFIX)` and `stripped.endswith("-->")` are considered markers. A mention mid-sentence is correctly ignored.

### 3. Idempotency Under Re-Post

**Risk**: Posting the same comment twice (e.g., after validation) double-prepends the marker.

**Mitigation**: Check `body.lstrip().startswith(SEVERITY_MARKER_PREFIX)` before prepending. If the marker already exists, return the body unmodified.

### 4. Human Threads Never Downgraded

**Risk**: A GO with open human threads is incorrectly downgraded.

**Mitigation**: The gate has a **separate guard** that checks `human_unresolved` first. Any human threads always cause a FINISH_FAIL with explanatory comment, regardless of automation threads. This is unchanged from pre-#1856.

## Integration Checklist

When implementing this pattern in a new automation context:

1. ✅ Define severity constants (BLOCKING_SEVERITIES, VALID_SEVERITIES, SEVERITY_MARKER_PREFIX)
2. ✅ Implement marker embedding with fail-safe default (unknown = blocking)
3. ✅ Implement severity extraction with line-prefix anchoring
4. ✅ Return 3-tuple (blocking, advisory, other) from thread counter
5. ✅ Gate on `blocking == 0`, not `total == 0`
6. ✅ Resolve advisory threads before arming/proceeding
7. ✅ Document thread resolution ordering to avoid merge-stage re-block
8. ✅ Test marker idempotency, substring false positives, and fail-safe defaults

## Related Issues

- **#1554** (original minor-thread deadlock): Demonstrated that advisory threads can deadlock when checked monolithically
- **#1856** (this fix): Severity-aware gate to split blocking vs advisory
- **#1575** (no-commit detection): Related thread-management improvement

## References

- ADR-001: Automation-Library Boundary (severity markers are part of automation product logic, not shared utilities)
- `docs/AUTOMATION_LOOP_ARCHITECTURE.md` section "5. pr_review" — verdict contract and GO semantics
- `hephaestus/automation/_review_phase.py:155` — legacy `_review_thread_count_decreased` (progress-aware extension)
- `hephaestus/automation/_review_phase.py:314` — legacy `_evaluate_go_verdict` (verdict semantics, re-housed in pr_review.py)
