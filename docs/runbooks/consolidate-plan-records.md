# Consolidating duplicate canonical plan records

Use this runbook when one issue carries two qualifying plan/review pairs and
the automation round stops on a planning-marker identity conflict. The known
shape (Comet #705): one actor published one pair through each tool, so the
journal holds four automation-owned comments and no code path may pick a
winner.

The command is read-only by default. It never edits the issue body, never
touches another actor's comments, and never reuses a historical GO.

## 1. Read the inventory

```bash
uv run python scripts/consolidate_plan_records.py \
  --repo HomericIntelligence/Hephaestus \
  --issue 705 \
  --keep-plan-comment 5446247620 \
  --keep-review-comment 5446247883 \
  --supersede-plan-comment 5444284559 \
  --supersede-review-comment 5444310901 \
  --plan-body-file merged-plan.md \
  --review-body-file merged-review.md
```

The dry run prints the pure claim inventory, the provenance header it will
seal, the archives, the deletes, and the label transition. It performs no
mutation.

## 2. Author the merged content

The tool does not merge content. Decide which plan survives, fold the other
plan's requirements into it, and fold both reviews' findings into one review
body. Save them to two files and pass them with `--plan-body-file` and
`--review-body-file`.

The review file must not end with a state token such as `state:plan-go`;
the consolidation epoch must force a fresh independent review, and the
command aborts when the file carries one.

## 3. Apply and verify

Re-run the same command with `--apply`. The executor order is:

1. re-verify the journal, issue body, and labels against the plan;
2. append one fenced history archive per superseded comment, read back;
3. re-verify every source digest and the issue requirements;
4. delete each superseded comment, read back after each delete;
5. upsert the keep plan and keep review in place through the guarded
   actor-owned upsert;
6. move a historical `state:plan-go` to `state:plan-no-go`;
7. read back the final state: exactly one owned plan claim and at most one
   owned review claim.

The provenance header inside the consolidated plan binds the issue, the
requirements digest, the epoch, and every source record (comment id, author,
marker alias, content digest). A historical GO label is never transferred.

## 4. Recover a partial run

Every step skips work that an earlier run already finished, so re-running
the same command converges: archives already present are skipped, deletes
already confirmed are skipped, and a completed consolidation reports
`already consolidated` without mutating anything. An unconfirmed delete
stops the run and names the superseded comments still to confirm; their
content already survives in the archives, so no evidence is lost.

An operator latch (`state:skip` or `state:blocked`) refuses the
consolidation. Remove the latch first.

## 5. Companion contract

Athena reads the same shared `HomericIntelligence` markers, so a conforming
Athena tolerates the unknown `plan-consolidation` header line as inert
metadata. The cross-tool reader contract is documented in the Athena
repository; link the companion Athena pull request from the Hephaestus PR
description.
