# AGENTS.md

This file is a single-page map of the AI-agent topology and conventions used by
Hephaestus and the wider HomericIntelligence ecosystem. For project-specific
rules (commit policy, branch naming, version model) see [`CLAUDE.md`](CLAUDE.md);
for enabled skill plugins see [`.claude/settings.json`](.claude/settings.json),
and for invocation guidance see the [`CLAUDE.md` skill catalog](CLAUDE.md#skill-catalog).

## Agents the codebase orchestrates

The default `hephaestus-automation-loop` path is the queue-based in-process
pipeline in `hephaestus.automation.pipeline.coordinator`. The coordinator owns
seven in-memory stage queues and dispatches agent/build/git jobs to a worker
pool. Each agent job runs either **Claude Code** or **Codex**, chosen via the
optional `--agent` CLI flag or auto-detected with a Claude preference when
omitted (see `hephaestus.agents.runtime.add_agent_argument`).

**Loop-owned approval policy:** `pr_review` invokes `$athena:pr-review` with
its normal default behavior when available, otherwise uses its inline-review
fallback. It posts inline findings and a final grade/GO-NOGO review; a GO
applies `state:implementation-go`. Normal review may collect CI/CD evidence
and incorporate it into its binary verdict, but the loop does not change CI/CD
and no workflow, status, artifact, or lease independently authorizes it.
`merge_wait` is the sole automatic armer and consumes that loop-owned label.

| Queue stage | Module | Purpose |
|-------------|--------|---------|
| repo | `hephaestus.automation.pipeline.stages.repo` | Clone/discover, classify issues/PRs, and seed entry queues |
| planning | `hephaestus.automation.pipeline.stages.planning` | Advise and produce an implementation plan |
| plan_review | `hephaestus.automation.pipeline.stages.plan_review` | Strict plan review, amendment, and plan labels |
| implementation | `hephaestus.automation.pipeline.stages.implementation` | Worktree creation, implementation, tests, commit/push, and PR creation |
| pr_review | `hephaestus.automation.pipeline.stages.pr_review` | Inline PR review, validation, comment addressing, and implementation labels |
| merge_wait | `hephaestus.automation.pipeline.stages.merge_wait` | Sole automatic armer for loop-approved PRs; preserves post-merge learn |
| finished | `hephaestus.automation.pipeline.stages.finished` | Terminal ledger and worktree cleanup/preservation |

Console scripts preserve their historical names. Stage-scoped wrappers are
thin queue-pipeline scoped entry points over the coordinator; manual commands
that do not map to a pipeline stage remain out-of-band tools:

| Console script | Current module | Purpose |
|----------------|----------------|---------|
| `hephaestus-plan-issues` | `hephaestus.automation.planner` | Thin queue-pipeline planning/plan_review wrapper |
| `hephaestus-implement-issues` | `hephaestus.automation.implementer` | Thin queue-pipeline implementation/pr_review/merge_wait wrapper |
| `hephaestus-merge-prs` | `hephaestus.github.pr_merge` | Manual merge-driving command outside the queue coordinator |
| `hephaestus-review-prs` | `hephaestus.automation.pr_reviewer` | Thin queue-pipeline pr_review wrapper |
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
and plan/review quality gates were first incubated before being generalized into
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
| `pr_review_core.py:_invoke_and_parse_review_session` | `Read,Glob,Grep,Bash,Skill,Agent,WebFetch` | Worktree PR analysis invokes the normal read-only `$athena:pr-review` workflow when available (or its inline fallback); the agent does not post reviews or mutate CI/CD. |
| `pipeline/stages/pr_review.py:PrReviewStage._review_wait` | `Read,Glob,Grep,Bash,Skill,Agent,WebFetch` | The sole pipeline GO/NOGO review uses the read-only AgentJob policy and may invoke the normal read-only `$athena:pr-review` workflow; validation and difficulty jobs keep `Read,Glob,Grep`. |
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
instructions that bypass the PR review loop. See the tests in
`tests/unit/automation/test_prompts.py` for the regression coverage.

## Human-in-the-loop checkpoints

Several plugin-provided skills mandate human gates that the agents must wait on:

- `/athena:myrmidon-swarm` — explicit Phase 1 "STOP HERE. Ask the user…"
  before any swarm deploys.
- `/athena:skill-advisor` — invoked at the start of any substantive task
  with `allowed-tools: []`, so it can route but never act autonomously.
- `/athena:finish-branch` and `/athena:code-review` — explicit confirm
  steps before tagging, force-pushing, or merging.

Every PR opened by the automation pipeline goes through GitHub's normal branch
protection and the `pr-policy` required-check gate
(see [`CLAUDE.md`](CLAUDE.md#pr-policy)) — a human still reviews and merges.

## Skill catalog

The Athena plugins enabled in `.claude/settings.json` provide 23 reusable skills
the agents can invoke. See [`CLAUDE.md`](CLAUDE.md#skill-catalog) for the full
table. Highlights:

- **Workflow**: `skill-advisor`, `advise`, `brainstorm`, `test-driven-development`,
  `systematic-debugging`, `verification`, `finish-branch`, `code-review`.
- **Repo audits**: `repo-analyze` and its `-quick`, `-strict`, `-full`, and
  `*-full` variants.
- **Worktrees**: `git-worktrees`, `worktree-cleanup`, `tidy`.
- **Orchestration**: `myrmidon-swarm` for hierarchical multi-agent fan-out.
- **Knowledge capture**: `learn` (writes back to the Mnemosyne marketplace).

## Configuration / boundaries

- Skill hooks, frontmatter, and per-skill `allowed-tools` are owned by the
  installed Athena plugins; `.claude/settings.json` is the repository-local
  source of truth for plugin enablement.
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

## Canonical architecture reference

The **canonical unified reference** for the queue-pipeline, stage semantics, ROUTES table, scope trimming, durable journal, worker pool, and observability lives at [\`docs/architecture.md\`](../docs/architecture.md). Update the doc (not this file) when the topology changes; this file remains the agent-topology map and skill catalog.

