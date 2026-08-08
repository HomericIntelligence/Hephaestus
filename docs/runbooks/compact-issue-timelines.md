# Compacting automation-owned issue timelines

Use this migration after upgrading from a release that appended plan/review
history, skip-reason comments, or implementation-reply handoff records to the
linked issue.

The command is read-only by default and scans open and closed issues. It skips
pull requests, preserves issue bodies, and ignores every comment that GitHub
does not prove belongs to the authenticated actor.

```bash
uv run python scripts/compact_issue_timelines.py \
  --repo HomericIntelligence/Hephaestus
```

Review every proposed comment ID, then apply the same inventory:

```bash
uv run python scripts/compact_issue_timelines.py \
  --repo HomericIntelligence/Hephaestus \
  --apply
```

Use `--state open` or `--state closed` to narrow the inventory, and `--issues
123 456` for a recovery scope. Apply mode re-reads each changed issue and fails
that issue if the canonical two-comment state did not converge. A malformed or
conflicting legacy journal is reported and skipped without deleting anything.

The expected final automation surface on a linked issue is:

1. the latest canonical implementation plan;
2. the latest canonical plan review.

Human comments and the original issue body remain untouched. PR review threads
and commit history retain detailed implementation evidence.
