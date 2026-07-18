# AGENTS.md

This file is a single-page map of the AI-agent topology and conventions used by
Hephaestus and the wider HomericIntelligence ecosystem. For project-specific
rules (commit policy, branch naming, version model) see [`CLAUDE.md`](CLAUDE.md);
for the catalog of skills the agents invoke, see the [`skills/`](skills/) directory.

## Agents the codebase orchestrates

The default `hephaestus-automation-loop` path is the queue-based in-process
pipeline in `hephaestus.automation.pipeline.coordinator`. The coordinator owns
eight in-memory stage queues and dispatches agent/build/git jobs to a worker
pool. Each agent job runs either **Claude Code** or **Codex**, chosen via the
optional `--agent` CLI flag or auto-detected with a Claude preference when
omitted (see `hephaestus.agents.runtime.add_agent_argument`).

**Loop-owned approval policy:** `strict_review` runs the read-only
`$athena:pr-review` skill between `pr_review` and `merge_wait`. After a
current-head GO, `strict_review` applies `state:implementation-go` itself.
The loop never reads, changes, or relies on CI/CD. `merge_wait` is the sole
automatic armer and uses that label only with the direct current-head handoff
from `strict_review`; a restart repeats the loop review. No workflow, status,
artifact, or lease authorizes it.

| Queue stage | Module | Purpose |
|-------------|--------|---------|
| repo | `hephaestus.automation.pipeline.stages.repo` | Clone/discover, classify issues/PRs, and seed entry queues |
| planning | `hephaestus.automation.pipeline.stages.planning` | Advise and produce an implementation plan |
| plan_review | `hephaestus.automation.pipeline.stages.plan_review` | Strict plan review, amendment, and plan labels |
| implementation | `hephaestus.automation.pipeline.stages.implementation` | Worktree creation, implementation, tests, commit/push, and PR creation |
| pr_review | `hephaestus.automation.pipeline.stages.pr_review` | Inline PR review, validation, comment addressing, and implementation labels |
| strict_review | `hephaestus.automation.pipeline.stages.strict_review` | Read-only Codex `$athena:pr-review` pass for the current PR head; applies loop-owned approval |
| merge_wait | `hephaestus.automation.pipeline.stages.merge_wait` | Sole automatic armer after the direct current-head review handoff; preserves post-merge learn |
| finished | `hephaestus.automation.pipeline.stages.finished` | Terminal ledger and worktree cleanup/preservation |

Console scripts preserve their historical names. Stage-scoped wrappers are
thin queue-pipeline scoped entry points over the coordinator; manual commands
that do not map to a pipeline stage remain out-of-band tools:

| Console script | Current module | Purpose |
|----------------|----------------|---------|
| `hephaestus-plan-issues` | `hephaestus.automation.planner` | Thin queue-pipeline planning/plan_review wrapper |
| `hephaestus-implement-issues` | `hephaestus.automation.implementer` | Thin queue-pipeline implementation/pr_review/strict_review wrapper |
| `hephaestus-merge-prs` | `hephaestus.github.pr_merge` | Manual merge-driving command outside the queue coordinator |
| `hephaestus-review-prs` | `hephaestus.automation.pr_reviewer` | Thin queue-pipeline internal pr_review wrapper; strict review runs in the implementation/drive-green slices |
| `hephaestus-agent-stage` | `hephaestus.automation.agent_stage` | One-off stage invocation |

## Agent runtime

`hephaestus.agents.runtime` is the thin layer that abstracts over Claude Code and
Codex. It provides:

- `add_agent_argument(parser)` — adds a uniform `--agent` flag to any CLI.
- `is_codex(agent_str)` — branches between the two providers.
- `run_codex_text(...)`, `run_codex_session(...)`, `resume_codex_session(...)` —
  invoke Codex.
- Claude is normally invoked via `hephaestus.automation.claude_invoke.invoke_claude_with_session`;
  the library-only fleet-sync conflict fallback uses `claude_code_sdk` with the scoped call-site
  controls below.

Per-agent model/session/timeout configuration is centralised in
`hephaestus.automation.agent_config`, all operator-tunable via explicit CLI flags
on each automation command (e.g., `--agent-timeout`, `--poll-max-wait`,
`--git-message-timeout`, etc.). Legacy `claude_models`, `claude_timeouts`, and
`session_naming` modules remain compatibility shims over `agent_config`.

The automation loop also accepts `--planner-reasoning-effort`,
`--implementer-reasoning-effort`, and `--reviewer-reasoning-effort` for Codex
roles. Values are `default`, `low`, `medium`, `high`, or `xhigh`; `default`
omits Codex's `model_reasoning_effort` setting. An omitted flag preserves the
selected model alias's established reasoning default.

## Design Philosophy

The agent topology above is not accidental — it follows a small set of design
principles inherited from **ProjectOdyssey**, where the queue-based agent loop
and strict plan/review gates were first incubated before being generalized into
Hephaestus's shared tooling. Those principles, applied to agent design, are:

- **Simplicity first (KISS / YAGNI).** Each queue stage owns one responsibility
  and one reason to change; we do not add stages, providers, or abstractions
  until a concrete workflow needs them. The deferred `AgentProtocol` and
  resilience wiring (issues #468, #469) are intentionally *not* built yet.
- **One-way dependencies (DRY / boundaries).** The dependency arrow points only
  automation → library (see [`CLAUDE.md`](CLAUDE.md#library-vs-product-layer)).
  Prompt construction lives in exactly one module (`hephaestus.automation.prompts`)
  so untrusted-content fencing is defined once, not per call site.
- **Substitutable providers (SOLID).** `hephaestus.agents.runtime` abstracts over
  Claude Code and Codex behind a uniform `--agent` flag so either provider is
  substitutable at a call site without changing orchestration logic.
- **Least privilege, least astonishment (POLA).** Every agent call site declares
  an explicit `--allowedTools` scope (see the permission-policy table below),
  runs in a scoped worktree, and defers all irreversible actions (merge, tag,
  force-push) to human-gated checkpoints.
- **Human-in-the-loop by default.** Autonomy is bounded: skills that can act
  destructively stop for a human gate, and every automation PR still passes
  branch protection and the `pr-policy` check.

For the full, non-agent-specific statement of these principles see
[`CLAUDE.md`](CLAUDE.md#key-development-principles).

## Claude non-interactive permission policy

Claude invocations that pass `permission_mode="dontAsk"` are non-interactive
automation calls. They do not use `--dangerously-skip-permissions`, and
`hephaestus.automation.claude_invoke.invoke_claude_with_session` still forwards
the explicit `--allowedTools` scope. There is no OS-level seccomp, namespace, or chroot sandbox on this Claude path. The compensating controls are per-call tool
allowlists, cwd/worktree scoping, subprocess timeouts, prompt fencing for
untrusted GitHub content, secure logs, and GitHub branch protection plus human
review before merge.

| Call site | Tools | Scope / controls |
| --- | --- | --- |
| `audit_reviewer.py:run_audit_coordinator` | `Read,Glob,Grep` | Repo-root audit analysis; no write tools; direct-runner parity uses `sandbox="read-only"`. |
| `review_validator.py:_run_validation_session` | `Read,Glob,Grep` | Worktree validation of prior review comments; no write tools; GitHub updates stay in orchestrator code. |
| `comment_difficulty.py:_run_classifier_session` | `Read,Glob,Grep` | Worktree comment classification; no write tools; result is parsed JSON only. |
| `pr_review_core.py:_invoke_and_parse_review_session` | `Read,Glob,Grep` | Worktree PR analysis (invoked once, or twice on a `PromptTooLongError` retry with a smaller diff budget, #1847); no write tools; review posting is handled outside the agent call. |
| `pipeline/stages/strict_review.py:StrictReviewStage` | `Read,Glob,Grep,Bash,Agent,Skill,WebFetch` | `$athena:pr-review` is dispatched through Codex in a synchronized `AgentJob(sandbox="read-only")`, because Claude's non-interactive tool surface cannot technically constrain the skill's required Bash evidence collection. The worker verifies its local HEAD matches the captured remote SHA and has no tracked or untracked changes. |
| `_implement_phase.py:ImplementPhase._run_claude_impl_session` | `Read,Write,Edit,Glob,Grep,Bash` | Initial implementation runs in the isolated issue worktree and remains subject to review and branch protection. |
| `_review_phase.py:ReviewPhase._resume_impl_with_feedback` | `Read,Write,Edit,Glob,Grep,Bash` | Review-feedback fixes resume the implementer in the isolated issue worktree and cannot bypass PR review or merge gates. |
| `address_review_core.py:run_address_fix_session` | `Read,Write,Edit,Glob,Grep,Bash,Task,Skill` | Review-thread fixes run in the isolated issue worktree; `Task`/`Skill` support per-comment sub-agents and skill-advisor routing. |
| `github/fleet_sync/conflict_resolver.py:_run_conflict_agent` | `none` | Claude-only conflict planner receives only nonce-fenced conflict text and returns JSON edits; direct runtimes are rejected because their tool surfaces cannot provide the zero-tool contract, no agent invocation occurs in `--dry-run`, and the host validates/writes only known paths, owns all Git continuation/signing/push, snapshots remote URLs, and pins the final lease to the discovered branch SHA. |

Fleet-sync `--dry-run` is a preview contract: GitHub reads and writes, Git subprocesses, pushes,
merges, and agent calls are suppressed or logged. The CLI may still allocate an ephemeral
temporary directory and pass Git actions through the dry-run logger so operators can see what
would run; no clone, worktree, rebase, or other Git mutation is executed.

## Prompt safety

`hephaestus.automation.prompts` builds every prompt the agents see. The module's
contract — enforced by the test suite — is that **all untrusted GitHub content**
(issue bodies, PR diffs, reviewer comments, plan text) is wrapped with
`_fence_untrusted()` using random nonces and accompanied by `_UNTRUSTED_NOTICE`.
This prevents a hostile issue body from forging a verdict line or injecting
instructions that bypass the strict review loop. See the tests in
`tests/unit/automation/test_prompts.py` for the regression coverage.

## Human-in-the-loop checkpoints

Several skills mandate human gates that the agents must wait on:

- `skills/myrmidon-swarm/SKILL.md` — explicit Phase 1 "STOP HERE. Ask the user…"
  before any swarm deploys.
- `skills/skill-advisor/SKILL.md` — invoked at the start of any substantive task
  with `allowed-tools: []`, so it can route but never act autonomously.
- `skills/finish-branch/SKILL.md`, `skills/code-review/SKILL.md` — explicit confirm
  steps before tagging, force-pushing, or merging.

Every PR opened by the automation pipeline goes through GitHub's normal branch
protection and the `pr-policy` required-check gate
(see [`CLAUDE.md`](CLAUDE.md#pr-policy)) — a human still reviews and merges.

## Skill catalog

`skills/` contains 23 reusable skills the agents can invoke. See
[`CLAUDE.md`](CLAUDE.md#skill-catalog) for the full table. Highlights:

- **Workflow**: `skill-advisor`, `advise`, `brainstorm`, `test-driven-development`,
  `systematic-debugging`, `verification`, `finish-branch`, `code-review`.
- **Repo audits**: `repo-analyze` and its `-quick`, `-strict`, `-full`, and
  `*-full` variants.
- **Worktrees**: `git-worktrees`, `worktree-cleanup`, `tidy`.
- **Orchestration**: `myrmidon-swarm` for hierarchical multi-agent fan-out.
- **Knowledge capture**: `learn` (writes back to the Mnemosyne marketplace).

## Configuration / boundaries

- Hooks and per-skill `allowed-tools` are declared in each skill's frontmatter
  (`skills/<name>/SKILL.md`) — these are the agent permission boundaries.
- `.claude/settings.json` carries project-level plugin enablement.
- **MCP** (Model Context Protocol): `.mcp.json` is the version-controlled
  configuration surface for optional project-scoped agent tooling and remains
  intentionally empty. MCP is not a Hephaestus runtime API or ecosystem
  transport; package and automation operation must not depend on it.
  Plugin marketplaces, NATS JetStream, and HTTP REST remain the maintained
  integration contracts. See [`docs/mcp.md`](docs/mcp.md) and
  [ADR-0011](docs/adr/0011-mcp-integration-posture.md).
- The deferred follow-ups for cross-agent abstraction (a formal `AgentProtocol`)
  and for wiring `hephaestus.resilience` into the GitHub call path are tracked
  in issues #468 and #469.
