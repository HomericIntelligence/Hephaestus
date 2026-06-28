---
name: learn
description: Save session learnings as a skill plugin — amends existing skills when the topic matches, creates new ones otherwise. Use after experiments, debugging sessions, or when you want to preserve team knowledge.
allowed-tools: [Read, Write, Edit, Bash, Grep, Glob]
---
<!-- markdownlint-disable MD025 -->

# /learn

Capture session learnings and amend an existing skill or create a new one in the ProjectMnemosyne marketplace. Always searches for an existing skill to update before creating a new file.

## Target Repository

**Repository**: resolved per gh-authenticated user — the user's own
`<gh-login>/ProjectMnemosyne` fork when available, otherwise upstream
`HomericIntelligence/ProjectMnemosyne`. Branches are pushed and PRs are opened
against the **resolved repository (the fork itself)**, not upstream.
**Base branch**: `main`
**Clone location**: `$HOME/.agent-brain/ProjectMnemosyne/`

Single shared clone in the user's home directory. Skill branches are created in
temporary worktrees (`/tmp/mnemosyne-skill-<name>`) for isolation — the shared
clone stays on main. Worktrees are cleaned up after PR creation. Automatically
detected if already running inside a ProjectMnemosyne checkout.

Resolution ladder (mirrors `hephaestus.github.mnemosyne_repo.resolve_mnemosyne_target`):

1. Reuse an existing `$HOME/.agent-brain/ProjectMnemosyne` checkout as-is.
2. Else clone `<gh-login>/ProjectMnemosyne` if it exists on GitHub.
3. Else fork `HomericIntelligence/ProjectMnemosyne` into the gh user's
   namespace, then clone the fork. If the gh user **is** `HomericIntelligence`,
   clone upstream directly (cannot fork a repo into its own org).

Override the resolved owner with the `HEPH_MNEMOSYNE_OWNER` environment variable.

## Execution Model

**CRITICAL: Always delegate /learn execution to a sub-agent.**

The `/learn` workflow modifies a **different repository** (ProjectMnemosyne) than the user's current
working directory. To avoid polluting the main conversation's git state or blocking other work:

1. **Use a sub-agent** — Delegate the entire skill creation/amendment workflow to a sub-agent so
   the main conversation can continue. The sub-agent handles clone/worktree setup, file creation,
   validation, commit, push, and PR creation autonomously.

2. **Use worktree isolation** — The sub-agent works in a git worktree (not the shared clone
   directly) to avoid branch conflicts if multiple `/learn` invocations run concurrently or if
   `/advise` is reading the same clone.

```python
# Launch /learn as an isolated sub-agent
Agent(
    description="Create/amend skill in ProjectMnemosyne",
    isolation="worktree",  # Isolated copy — no branch conflicts
    prompt="""Execute the /learn workflow for ProjectMnemosyne:
    1. Resolve the target repo (the gh user's own <login>/ProjectMnemosyne fork,
       created if needed, else upstream) and clone/update it at
       $HOME/.agent-brain/ProjectMnemosyne — see the Setup step's
       resolve_mnemosyne_target helper
    2. Search for existing skills to amend
    3. Create/amend the skill file in a worktree branch
    4. Validate with scripts/validate_plugins.py
    5. Commit, push, create PR against the resolved repo, enable auto-merge
    6. Clean up the worktree

    Session learnings to capture: <extracted learnings from conversation>"""
)
```

**Why sub-agents + worktrees:**

- The shared clone at `$HOME/.agent-brain/ProjectMnemosyne/` may be on a different branch from a
  prior `/learn` or `/advise` invocation
- Direct `git checkout -b` in the shared clone can fail if another agent is mid-operation
- Worktree isolation guarantees a clean state for each skill PR
- Sub-agent delegation keeps the main conversation free to continue other work

## Instructions

When the user invokes this command:

1. **Analyze the conversation** to extract:
   - Objective: What was the user trying to accomplish?
   - Steps taken: What approaches were tried?
   - Successes: What worked?
   - Failures: What didn't work and why?
   - Parameters: What configs/settings were used?

**CRITICAL — One skill per independent learning:**

If the session produced multiple learnings that are independently useful and would be searched for with different keywords, create separate skills for each rather than combining them into one. Ask yourself: "Would someone searching for learning A ever think to look in a skill about learning B?" If no, they must be separate files.

Examples of learnings that **must be split**:

- A git structural fix (symlink→submodule conversion) + a build system pattern (BUILD_ROOT) + a rebase strategy → 3 skills
- A training hyperparameter finding + a Docker isolation fix → 2 skills

Examples that **can stay combined**:

- Two failure modes of the same API call → 1 skill (same search surface)
- A configuration flag and its required companion setting → 1 skill

When splitting, launch parallel sub-agents (one per skill) rather than creating them sequentially:

```python
# If multiple independent learnings, launch one agent per skill in parallel:
Agent(description="Create skill A", prompt="...skill A content...")
Agent(description="Create skill B", prompt="...skill B content...")
Agent(description="Create skill C", prompt="...skill C content...")
```

1. **Auto-generate skill metadata** (NO user prompting):
   - Analyze conversation topic to extract: `<topic>-<subtopic>`
   - Generate short 4-word summary from key learning
   - Filename: `<topic>-<subtopic>-<short-4-word-summary>` (kebab-case)
   - Auto-detect category from conversation context (training, evaluation, optimization, debugging, architecture, tooling, ci-cd, testing, documentation)

2. **CRITICAL — Search for existing skills to amend**:

   Before creating a new file, search the registry for skills covering the same topic:

   ```bash
   # Resolve the target slug (defined in the Setup step below) so the amend-lock
   # check queries the same repo we will push to. The clone itself is created in
   # the Setup step; if it already exists, search it now.
   TARGET_SLUG="${TARGET_SLUG:-$(
     owner="${HEPH_MNEMOSYNE_OWNER:-$(gh api user --jq .login 2>/dev/null)}"
     if [ -z "$owner" ] || [ "$owner" = "HomericIntelligence" ]; then
       echo "HomericIntelligence/ProjectMnemosyne"
     else
       echo "$owner/ProjectMnemosyne"
     fi
   )}"
   MNEMOSYNE_DIR="$HOME/.agent-brain/ProjectMnemosyne"
   # Search by keywords from the skill name (only if the clone is already present)
   ls "$MNEMOSYNE_DIR/skills/" | grep -i "<keyword1>\|<keyword2>\|<keyword3>" | grep -v ".notes.md" | grep -v ".history"
   # Also search descriptions in frontmatter
   grep -l "<keyword>" "$MNEMOSYNE_DIR/skills/"*.md 2>/dev/null | head -20
   ```

   **CRITICAL — Check for an OPEN PR already amending this skill (amend-lock):**

   Searching only the local `main` clone is NOT enough. Multiple agents run `/learn` in
   parallel; if each forks a fresh `origin/main` branch to amend the **same** skill, every
   branch edits the same `.md`/`.history` and they all become mutually conflicting (DIRTY).
   This is the #1 cause of the duplicate-PR pileup. Before creating any branch:

   ```bash
   # (a) Open PRs whose title names the skill
   gh pr list --repo "$TARGET_SLUG" --state open \
     --search "<name> in:title" --json number,headRefName,title
   # (b) Open PRs that touch the file even if the title differs
   gh pr list --repo "$TARGET_SLUG" --state open \
     --json number,headRefName,files \
     --jq '.[] | select(.files[].path == "skills/<name>.md") | {number, headRefName}'
   ```

   **Decision rule:**

   - **An open PR already amends this skill** → do **NOT** fork a new `origin/main` branch.
     Either (a) add your learning to that PR's existing `headRefName` (check it out, amend,
     push), or (b) if that branch is unavailable, create your worktree branch **from** it
     (`git worktree add <dir> -b skill/<name>-followup origin/<headRefName>`) so your change
     **stacks** on theirs instead of conflicting. Reference the existing PR number.
   - **No open PR** → proceed with the normal worktree-from-`origin/main` flow (Step 5).

   **If an existing skill covers the same topic → AMEND it** (don't create a new file):

   a. Read the existing skill to understand its current state
   b. Archive the current version to the history log (see Step 4)
   c. Update the skill `.md` in-place with new learnings:
      - Add new Failed Attempts rows to the table
      - Update the Verified Workflow if the approach changed
      - Update Results & Parameters with new data
      - Bump the `version` using **semantic versioning** (see table below)
      - Update the `date` to today

   **Semantic versioning rules for skill amendments:**

   | Change Type | Bump | When to Use | Examples |
   |-------------|------|-------------|----------|
   | **Major** (X.0.0) | `1.0.0` → `2.0.0` | Merge multiple skills, rewrite verified workflow, change core recommendation | Consolidating 5 duplicate skills; replacing recommended API |
   | **Minor** (0.X.0) | `1.0.0` → `1.1.0` | Add new findings, new failed attempts, extend workflow with new steps | Adding 2 Failed Attempts rows; new "When to Use" trigger |
   | **Patch** (0.0.X) | `1.0.0` → `1.0.1` | Fix typos, formatting, metadata corrections, clarify existing text | Fix category typo; fix broken markdown table |

   d. Update the changelog in the history file

   **If no existing skill matches → Create a new skill** (proceed to Step 5)

3. **History log management** (for amendments):

   When amending an existing skill, preserve the previous version in `skills/<name>.history`:

   **File: `skills/<name>.history`**

   This is an append-only log. Each entry records what changed and why. Format:

   ```markdown
   # <skill-name> — History

   ## v2.0.0 (YYYY-MM-DD)

   **Changed by:** Session context (e.g., "PR #5107 gradient checking fixes")
   **Verification:** verified-ci | verified-local | verified-precommit | unverified

   ### What changed
   - Updated tolerance from 1e-2 absolute to rtol=1e-2 + atol=1e-2 combined
   - Added check_gradient() as preferred API over check_gradients()
   - Added 2 new Failed Attempts entries

   ### Why
   Previous approach (v1.0.0) used check_gradients() with absolute tolerance.
   CI showed this fails for multi-channel conv2d where gradient magnitudes reach ~32-126.
   Relative tolerance via check_gradient() handles large magnitudes correctly.

   ### Previous version (v1.0.0) snapshot
   <paste the full previous skill content here as a reference>

   ---

   ## v1.0.0 (YYYY-MM-DD)

   **Initial version.**
   ```

   **Rules for history files:**
   - Append new entries at the TOP (newest first)
   - Always include: version, date, what changed, why, previous snapshot
   - The snapshot preserves the exact previous content for auditability
   - Add a reference from the main skill file: `**History:** [changelog](./skills/<name>.history)`

4. **CRITICAL — Honesty gate for "Verified Workflow"**:

   Before writing the "Verified Workflow" section, answer these questions honestly:
   - Was the workflow actually executed end-to-end? (Not just pre-commit hooks — the actual tests/code)
   - Did CI pass with these changes? If not, convey that **inside** the section (see below) — do
     NOT rename the header.
   - Were the results observed in CI, or only locally? If only locally, state: "Verified locally only — CI validation pending"

   **CRITICAL — the section header MUST stay `## Verified Workflow` regardless of verification
   level.** `validate_plugins.py` requires the literal string `## Verified Workflow` (it does a
   plain substring check for it); renaming it to `## Proposed Workflow` causes a hard
   `validate` failure: `Missing required section: ## Verified Workflow`. For an unverified or
   `verified-precommit` skill, keep the header and add a status note as the first line of the
   section instead (see Step 6).

   **Verification levels** (must be stated in the skill):
   - `verified-ci`: Tests pass in CI (highest confidence)
   - `verified-local`: Tests pass locally but not confirmed in CI
   - `verified-precommit`: Only pre-commit hooks pass (formatting, linting)
   - `unverified`: Approach is theoretically sound but never executed

   **CRITICAL — `verified-ci` is a claim about THIS PR's gate, not about local checks.**
   You may write `verified-ci` **only after** the skill PR's required gate
   (`validate` **and** `markdownlint`) is observed **green** on the open PR:

   ```bash
   gh pr view <PR> --repo "$TARGET_SLUG" \
     --json mergeStateStatus,statusCheckRollup \
     --jq '{state: .mergeStateStatus, failing: [.statusCheckRollup[] | select(.conclusion=="FAILURE") | .name]}'
   ```

   Passing `validate_plugins.py` + markdownlint **locally is `verified-local`, not
   `verified-ci`** — local runs do not exercise the full CI gate (ruff, mypy, pytest). Set
   the skill to `verified-local` at creation time, and only bump it to `verified-ci` once you
   have seen the gate go green. Do **not** label a skill `verified-ci` while its own PR is
   `BLOCKED`/`DIRTY` with a red `validate`/`markdownlint` — that is the overclaim that made the
   backlog's verification levels untrustworthy.

   Add this as a frontmatter field:

   ```yaml
   verification: verified-ci | verified-local | verified-precommit | unverified
   ```

5. **Setup repository using worktrees** (CRITICAL — always use worktrees for branch isolation):

   ```bash
   # resolve_mnemosyne_target: pick the owner/ProjectMnemosyne slug to clone/PR
   # against. Mirrors hephaestus.github.mnemosyne_repo.resolve_mnemosyne_target.
   # 1) HEPH_MNEMOSYNE_OWNER override; 2) gh login's own fork (create if needed);
   # 3) upstream when login is HomericIntelligence or undeterminable.
   resolve_mnemosyne_target() {
     local upstream="HomericIntelligence/ProjectMnemosyne"
     local owner="${HEPH_MNEMOSYNE_OWNER:-$(gh api user --jq .login 2>/dev/null)}"
     if [ -z "$owner" ] || [ "$owner" = "HomericIntelligence" ]; then
       echo "$upstream"; return
     fi
     if gh repo view "$owner/ProjectMnemosyne" --json name >/dev/null 2>&1; then
       echo "$owner/ProjectMnemosyne"; return
     fi
     if gh repo fork "$upstream" --clone=false >/dev/null 2>&1; then
       echo "$owner/ProjectMnemosyne"; return
     fi
     echo "$upstream"  # fork failed — fall back to upstream
   }

   # Detect if already inside a ProjectMnemosyne checkout (fast path).
   CURRENT_REMOTE=$(git remote get-url origin 2>/dev/null || echo "")
   if [[ "$CURRENT_REMOTE" == *"ProjectMnemosyne"* ]] && [[ "$CURRENT_REMOTE" != *"ProjectMnemosyne-"* ]]; then
     # Already in ProjectMnemosyne - use worktree from current repo
     MNEMOSYNE_DIR="."
     TARGET_SLUG="$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null)"
   else
     # Use shared home directory location
     MNEMOSYNE_DIR="$HOME/.agent-brain/ProjectMnemosyne"
     TARGET_SLUG="${TARGET_SLUG:-$(resolve_mnemosyne_target)}"

     if [ ! -d "$MNEMOSYNE_DIR" ]; then
       # Clone fresh (existing checkout is reused as-is, regardless of remote)
       mkdir -p "$HOME/.agent-brain"
       gh repo clone "$TARGET_SLUG" "$MNEMOSYNE_DIR"
       ( cd "$MNEMOSYNE_DIR" && ensure_precommit_installed )  # see helper below
     fi

     # Always update to latest main before starting
     git -C "$MNEMOSYNE_DIR" fetch origin
     git -C "$MNEMOSYNE_DIR" checkout main
     git -C "$MNEMOSYNE_DIR" pull --ff-only origin main

     # Verify pre-commit is genuinely installed; (re)install if not.
     ( cd "$MNEMOSYNE_DIR" && ensure_precommit_installed )
   fi
   ```

   **Robust pre-commit check (`ensure_precommit_installed`).** Do NOT test
   `[ -f "$DIR/.git/hooks/pre-commit" ]`: it is wrong two ways — (1) in a **worktree**, `.git`
   is a *file* (a `gitdir:` pointer), not a directory, so the path never exists even when hooks
   are installed; (2) a stray `core.hooksPath` (e.g. pointing back at the default `.git/hooks`)
   can make the file exist while `pre-commit install` was never run. Ask the resolved hook for
   the pre-commit signature instead (define this helper before the setup block above):

   ```bash
   ensure_precommit_installed() {
     # Resolve the hooks dir git actually uses (honors core.hooksPath and worktrees)
     local hooks_dir; hooks_dir="$(git rev-parse --git-path hooks)"
     # The pre-commit-managed hook contains a recognizable marker line.
     if ! grep -qs 'pre-commit' "$hooks_dir/pre-commit"; then
       pre-commit install --install-hooks
     fi
     # Final sanity check: config parses and a hook actually fires.
     pre-commit validate-config >/dev/null 2>&1 \
       || echo "WARNING: pre-commit config invalid — run 'pre-commit install --install-hooks' here"
   }

   # Create a worktree for branch isolation (never checkout branches in the base repo)
   WORKTREE_DIR="/tmp/mnemosyne-skill-<name>"
   git -C "$MNEMOSYNE_DIR" worktree add "$WORKTREE_DIR" -b skill/<name> origin/main
   cd "$WORKTREE_DIR"
   ```

   **Why worktrees?** Working directly on the shared clone switches its branch, which
   breaks concurrent `/advise` operations and leaves the clone in a dirty state if
   the session is interrupted. Worktrees provide isolated copies that are safe to
   abandon.

6. **Generate or amend skill file** as flat `skills/<name>.md`:

   > New flat format: Single `.md` file in `skills/` root (not nested directories or plugin.json)

   **File 1: `skills/<name>.md`** with **YAML frontmatter + markdown body**:

   ```yaml
   ---
   name: <skill-name>
   description: "Brief description of what this skill teaches. Use when: (1) trigger1, (2) trigger2."
   category: <category>
   date: YYYY-MM-DD
   version: "1.0.0"
   user-invocable: false
   verification: <verified-ci|verified-local|verified-precommit|unverified>
   history: <name>.history  # Only present if skill has been amended
   tags: []
   ---

   # Skill Title

   ## Overview

   | Field | Value |
   |-------|-------|
   | **Date** | YYYY-MM-DD |
   | **Objective** | What was this skill developed to accomplish? |
   | **Outcome** | Was it successful? Operational? |
   | **Verification** | verified-ci / verified-local / verified-precommit / unverified |
   | **History** | [changelog](./<name>.history) |

   ## When to Use

   - Trigger condition 1
   - Trigger condition 2

   ## Verified Workflow

   ### Quick Reference

   ```bash
   # Copy-paste ready commands
   command --flag value
   ```

   ### Detailed Steps

   1. Step 1 description
   2. Step 2 description

   ## Failed Attempts

   | Attempt | What Was Tried | Why It Failed | Lesson Learned |
   |---------|----------------|---------------|----------------|
   | Attempt 1 | Description | Why failed | Lesson |

   ## Results & Parameters

   [Copy-paste ready configs and expected outputs]

   ## Verified On

   | Project | Context | Details |
   |---------|---------|---------|
   | ProjectName | Session context | [notes.md](./skills/<name>.notes.md) |

   ```

   Rules:
   - Filename: lowercase kebab-case (`^[a-z0-9-]+$`) — e.g., `training-grpo-external-vllm-setup.md`
   - `category`: one of 9 valid categories (no "refactoring" — use "architecture")
   - All required fields in frontmatter: name, description, category, date, version, verification
   - All required markdown sections: Overview, When to Use, Verified Workflow, Failed Attempts, Results & Parameters
   - **If verification is `unverified` or `verified-precommit`**: **keep the `## Verified Workflow` header** (the validator requires that literal string — renaming to "Proposed Workflow" causes `Missing required section: ## Verified Workflow`). Instead, make the **first line of the section** a warning blockquote: "> **Warning:** This workflow has not been validated end-to-end. Treat as a hypothesis until CI confirms." Optionally add a `> _(Proposed — not yet verified)_` subtitle under the header.

   **File 2: `skills/<name>.notes.md`** (optional):
   - Raw session details, code snippets, debugging logs
   - Human-readable reference material
   - Only create if additional context needed beyond main skill file

   **File 3: `skills/<name>.history`** (created on first amendment):
   - Append-only changelog with version snapshots
   - Referenced from main skill file via `history` frontmatter field
   - See Step 4 for format

7. **Validate skill** (MUST pass before committing):

   ### Pre-Commit Validation Checklist

   Before running `validate_plugins.py`, verify:

   | # | Check | Error If Missing |
   |---|-------|------------------|
   | 1 | Skill is in `skills/<name>.md` (flat, NOT nested) | File in wrong location |
   | 2 | YAML frontmatter starts with `---` | "missing YAML frontmatter" |
   | 3 | Frontmatter has: name, description, category, date, version | "Missing required field: X" |
   | 4 | `category` is one of: training, evaluation, optimization, debugging, architecture, tooling, ci-cd, testing, documentation | "Invalid category" |
   | 5 | Markdown has all 5 sections: Overview, When to Use, Verified Workflow, Failed Attempts, Results & Parameters | "Missing required section" |
   | 6 | `## Failed Attempts` has pipe-delimited table | "Failed Attempts table missing required columns" |
   | 7 | `## Quick Reference` is subsection `### Quick Reference` (under Verified Workflow) | "Quick Reference should use ###" |
   | 8 | **Every markdown table's header pipe-count equals each body row's pipe-count**; a literal `\|` inside cell text is escaped | markdownlint `MD056/table-column-count` (CI-only gate, NOT caught by `validate_plugins.py`) |

   > **#8 is the single most common CI failure for skill PRs.** The Overview, Failed Attempts,
   > and Results tables are the usual offenders: an unescaped `|` inside a cell (e.g. a regex,
   > a shell pipe, or `a\|b`) is read as a column separator, so the row has more cells than the
   > header. Write inline pipes as `\|`. `validate_plugins.py` does **not** check this — only
   > the markdownlint gate does, so you MUST run markdownlint locally (below).

   Run **all three** checks from the worktree root before committing, in this order
   (markdownlint first — it is the gate most skill PRs fail on):

   ```bash
   # 0) Required-section self-check — guarantees validate_plugins.py won't fail with
   #    "Missing required section". Generate the file FROM the full template (Step 6) so all
   #    five headers exist by construction, then prove it before anything else:
   for sec in "## Overview" "## When to Use" "## Verified Workflow" \
              "## Failed Attempts" "## Results & Parameters"; do
     grep -qF "$sec" "skills/<name>.md" || echo "MISSING SECTION: $sec"
   done
   #    The header is ALWAYS literally "## Verified Workflow" — even for unverified skills the
   #    validator requires that exact string, so never rename it to "## Proposed Workflow"
   #    (that itself triggers "Missing required section: ## Verified Workflow"). Convey
   #    unverified status with a warning blockquote inside the section (Step 6), not the header.
   #    Any "MISSING SECTION" line = the validate gate WILL fail. Add the section before
   #    committing. This is the exact defect behind PRs that ADD a skill missing 4 sections.

   # 1) markdownlint — EXACTLY as CI runs it. It checks ALL rules, not just tables:
   #    MD056 (table-column-count) AND e.g. MD012 (no-multiple-blanks), MD013, MD040, etc.
   #    Run from the worktree root so it picks up the repo's .markdownlint.yaml.
   npx --yes markdownlint-cli2 --config .markdownlint.yaml "skills/<name>.md" "skills/<name>.history"
   #    Must exit 0. Fix every MDxxx error it reports — do not assume MD056 is the only one.
   #    Common offenders: MD056 (unbalanced table pipes), MD012 (≥2 consecutive blank lines).
   #    Do NOT add a markdownlint-disable comment to silence an error — the forbid-suppressions
   #    gate rejects blanket disables, and the defect is real. Fix the markdown instead.

   # 2) plugin validator — note this lints the ENTIRE skills/ dir, not just your file (see below)
   python3 scripts/validate_plugins.py

   # 3) pre-commit — runs the hooks CI also relies on (ruff, ruff-format, signed-commit check)
   pre-commit run --files "skills/<name>.md" "skills/<name>.history"
   ```

   If any of the three fails, fix errors and re-run. Do NOT commit until all pass.

   **Whole-repo validation (`validate_plugins.py` lints all of `skills/`):** if step 2 reports
   errors in files you did **not** touch, the `validate` gate is **already red on `main`** and
   your PR will inherit the red gate through no fault of your change. Likewise, run markdownlint
   over the whole dir (`npx --yes markdownlint-cli2 --config .markdownlint.yaml "skills/*.md"`)
   if your PR's `markdownlint` gate fails on a file you didn't edit. In either case: do **not**
   claim `verified-ci`, and surface the pre-existing breakage to the user — it needs its own
   fix PR; your amendment did not cause it.

8. **Commit and push**:

   ```bash
   # For new skills:
   git add skills/<name>.md skills/<name>.notes.md 2>/dev/null || true
   git commit -S -m "feat: add <name> skill

   Documents <brief description of what was learned>.

   Verification: <verified-ci|verified-local|verified-precommit|unverified>

   Key learnings:

   - <bullet 1>
   - <bullet 2>
   - <bullet 3>

   Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"

   # For amendments

   git add skills/<name>.md skills/<name>.history skills/<name>.notes.md 2>/dev/null || true
   git commit -S -m "feat: amend <name> skill (v<X.0.0>)

   <Brief description of what changed and why>.

   Verification: <level>
   Previous version archived in <name>.history

   Key changes:

   - <change 1>
   - <change 2>

   Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"

   git push -u origin skill/<name>

   ```

9. **Create PR** (only if push succeeded):

    ```bash
    gh pr create --repo "$TARGET_SLUG" --base main \
      --title "feat: <add|amend> <name> skill" \
      --body "## Summary

    <New skill | Amends existing skill from v<old> to v<new>>.

    Documents <brief description of what was learned>.

    - <Key point 1>
    - <Key point 2>
    - <Key point 3>

    ## Verification Level

    **<verified-ci|verified-local|verified-precommit|unverified>**

    <If not verified-ci, explain what is pending>

    ## Key Findings

    **What Worked**:
    - <Successful approach 1>
    - <Successful approach 2>

    **What Failed**:
    - <Failed attempt 1> → <Why it failed>

    ## Test Plan

    - [ ] Validate with \`python3 scripts/validate_plugins.py\`
    - [ ] Verify skill appears in marketplace
    - [ ] Test skill discovery with relevant keywords

    Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"

    # Enable auto-merge so the PR merges automatically once CI passes
    # Note: gh pr merge requires a PR number when using --repo
    PR_NUMBER=$(gh pr list --repo "$TARGET_SLUG" --head "skill/<name>" --json number --jq '.[0].number')
    HELPER=""
    for cand in \
        "${HEPHAESTUS_REPO_ROOT:-}/scripts/choose_merge_flag.sh" \
        "$(git rev-parse --show-toplevel 2>/dev/null)/scripts/choose_merge_flag.sh" \
        "$HOME/Projects/ProjectHephaestus/scripts/choose_merge_flag.sh"; do
        if [ -r "$cand" ]; then HELPER="$cand"; break; fi
    done
    if [ -n "$HELPER" ]; then
        . "$HELPER"
        MERGE_FLAG=$(choose_merge_flag "$TARGET_SLUG") || MERGE_FLAG="--squash"
    else
        MERGE_FLAG="--squash"
    fi
    gh pr merge "$PR_NUMBER" --auto "$MERGE_FLAG" --repo "$TARGET_SLUG"
    ```

10. **Cleanup worktree** (always clean up after PR creation):

    ```bash
    # Remove the worktree (keeps the shared clone intact for future /advise)
    git -C "$MNEMOSYNE_DIR" worktree remove "$WORKTREE_DIR" 2>/dev/null || rm -rf "$WORKTREE_DIR"
    git -C "$MNEMOSYNE_DIR" worktree prune
    ```

11. **Report honest status — enabling auto-merge is NOT "done":**

    The PR merges only when the required gate (`validate` + `markdownlint`) goes green. Until
    then it is `BLOCKED`. When reporting status, report the **actual** state and any failing
    checks — never assume success because auto-merge was armed:

    ```bash
    gh pr view "$PR_NUMBER" --repo "$TARGET_SLUG" \
      --json mergeStateStatus,statusCheckRollup \
      --jq '{state: .mergeStateStatus, failing: [.statusCheckRollup[] | select(.conclusion=="FAILURE") | .name]}'
    ```

    If `failing` is non-empty, the skill is at best `verified-local` — fix the checks (re-run
    markdownlint/validate locally per Step 7) before claiming completion. (Mirrors the
    auto-merge-after-go discipline: arming `--auto` does not by itself merge anything.)

## Amendment Workflow Summary

```text
Existing skill found?
├─ YES → Open PR already amends it? (gh pr list search, Step 2)
│   ├─ YES → Stack on that PR's branch (push to it, or branch FROM it). Do NOT fork main.
│   └─ NO  → Amend workflow:
│       1. Read existing skill
│       2. Create/append to <name>.history with previous version snapshot
│       3. Update <name>.md in-place (new data, bump version, update date)
│       4. Add history frontmatter field if first amendment
│       5. Commit both files
│
└─ NO → New skill workflow:
    1. Create <name>.md with full template
    2. Optionally create <name>.notes.md
    3. No history file needed yet
    4. Commit
```

> **Lesson (from the 2026-06-13 Mnemosyne backlog):** forking a fresh `origin/main` branch
> while ~8 other open PRs already amended the same skill (`python-module-decomposition`,
> `stale-documentation-audit`) produced ~35 mutually-DIRTY PRs — modify/modify conflicts on
> the same `.md`/`.history`. Always run the open-PR check in Step 2 and stack instead of fork.

## Common Issues & Solutions

### Top Validation Failures

| Error | Cause | Fix |
|-------|-------|-----|
| "Missing required field: X" | Frontmatter missing a field | Add field to YAML: name, description, category, date, version |
| "Invalid category" | Category not in approved list | Use one of: training, evaluation, optimization, debugging, architecture, tooling, ci-cd, testing, documentation |
| "missing YAML frontmatter" | Doesn't start with `---` | Add `---` at very top of file before metadata |
| "Missing required section: X" | Missing Overview/When/Workflow/Failed/Results | Add all 5 sections with `##` headers |
| "Failed Attempts table missing required columns" | Table format incorrect | Use: \| Attempt \| What Was Tried \| Why It Failed \| Lesson Learned \| |
| "Quick Reference should use ###" | Using `## Quick Reference` instead of `###` | Demote to `### Quick Reference` (subsection of Verified Workflow) |
| Skill not in marketplace | File not committed or in wrong location | Verify in `skills/<name>.md` (root of skills dir, not nested) |
| markdownlint `MD056/table-column-count` "Too many cells" | An unescaped literal `\|` inside a table cell (regex, shell pipe, `a\|b`) is parsed as a column separator | Escape inline pipes as `\|`, or balance the row so its cell count matches the header. **Not caught by `validate_plugins.py`** — only the CI `markdownlint` gate; run markdownlint locally first |
| markdownlint `MD012/no-multiple-blanks` | Two or more consecutive blank lines (often left between generated sections) | Collapse to a single blank line. Run markdownlint locally — it flags **all** rules (MD012, MD040, …), not just MD056 |
| `validate` "Missing required section: ## X" on **your own new file** | `/learn` generated the skill without all 5 required sections | Generate from the full Step 6 template; run the Step 7 required-section self-check (grep for all 5 `##` headers) before committing |
| `validate` "Missing required section: ## Verified Workflow" despite the section looking present | The header was renamed to `## Proposed Workflow` for an unverified skill — but `validate_plugins.py` substring-matches the literal `## Verified Workflow` | Always keep the `## Verified Workflow` header; put the "not yet verified" warning as a blockquote inside the section, never in the header |
| `validate`/`markdownlint` red on a file you didn't touch | A pre-existing broken file on `main` (the validator/linter scans the whole `skills/` dir) | Not your bug — surface it to the user as a separate fix PR; do not claim `verified-ci` while the gate is red |

### Issue: PR already exists

**Cause**: Branch was already pushed in previous attempt.

**Solution**: Either delete the branch and re-push, or update the existing PR:

```bash
# Delete old branch and try again
git push origin :skill/<name>
git push -u origin skill/<name>

# OR update existing PR
git push origin skill/<name>
```

### Issue: Cleanup directory

**Cause**: Shared clone at `$HOME/.agent-brain/ProjectMnemosyne` takes up disk space.

**Solution**: Safe to delete anytime — re-clones automatically on next `/advise` or `/learn`:

```bash
rm -rf $HOME/.agent-brain/ProjectMnemosyne
```

## Required Sections

| Section | Format | Purpose |
|---------|--------|---------|
| **YAML frontmatter** | Starts with `---`, includes name/description/category/date/version/verification | Metadata for marketplace |
| **Overview** | `## Overview` with table (date, objective, outcome, verification) | Quick context |
| **When to Use** | Bullet points with trigger conditions | Discoverability |
| **Verified Workflow** | Steps that worked + `### Quick Reference` subsection | The actual solution |
| **Failed Attempts** | Table: Attempt, What Was Tried, Why Failed, Lesson | Prevent wasted effort |
| **Results & Parameters** | Copy-paste configs, expected outputs | Actionable reference |

## Example

```
/hephaestus:learn
```

Claude will analyze the session, check for existing skills to amend, and either update an existing skill (with history) or create a new one.
