# AGENTS.md

This file is a single-page map of the AI-agent topology and conventions used by
Hephaestus and the wider HomericIntelligence ecosystem. For project-specific
rules (commit policy, branch naming, version model) see [`CLAUDE.md`](CLAUDE.md).

## Agents the codebase orchestrates

The default `hephaestus-automation-loop` path is the queue-based in-process
pipeline in `hephaestus.automation.pipeline.coordinator`. The coordinator owns
eight in-memory stage queues and dispatches agent/build/git jobs to a worker
pool. Each agent job runs either **Claude Code** or **Codex**, chosen via the
optional `--agent` CLI flag or auto-detected with a Claude preference when
omitted (see `hephaestus.agents.runtime.add_agent_argument`).

| Queue stage | Module | Purpose |
|-------------|--------|---------|
| repo | `hephaestus.automation.pipeline.stages.repo` | Clone/discover, classify issues/PRs, and seed entry queues |
| planning | `hephaestus.automation.pipeline.stages.planning` | Advise and produce an implementation plan |
| plan_review | `hephaestus.automation.pipeline.stages.plan_review` | Strict plan review, amendment, and plan labels |
| implementation | `hephaestus.automation.pipeline.stages.implementation` | Worktree creation, implementation, tests, commit/push, and PR creation |
| pr_review | `hephaestus.automation.pipeline.stages.pr_review` | Inline PR review, validation, comment addressing, and implementation labels |
| ci | `hephaestus.automation.pipeline.stages.ci` | Non-blocking CI classification and CI-fix routing |
| merge_wait | `hephaestus.automation.pipeline.stages.merge_wait` | Auto-merge arming, merge polling, dirty/blocked handling, and post-merge learn |
| finished | `hephaestus.automation.pipeline.stages.finished` | Terminal ledger and worktree cleanup/preservation |

Console scripts preserve their historical names. Stage-scoped wrappers are
thin queue-pipeline scoped entry points over the coordinator; manual commands
that do not map to a pipeline stage remain out-of-band tools:

| Console script | Current module | Purpose |
|----------------|----------------|---------|
| `hephaestus-plan-issues` | `hephaestus.automation.planner` | Thin queue-pipeline planning/plan_review wrapper |
| `hephaestus-implement-issues` | `hephaestus.automation.implementer` | Thin queue-pipeline implementation/pr_review wrapper |
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
| `pr_review_core.py:run_pr_review_analysis` | `Read,Glob,Grep` | Worktree PR analysis; no write tools; review posting is handled outside the agent call. |
| `_implement_phase.py:ImplementPhase._run_claude_impl_session` | `Read,Write,Edit,Glob,Grep,Bash` | Initial implementation runs in the isolated issue worktree and remains subject to review, CI, and branch protection. |
| `_review_phase.py:ReviewPhase._resume_impl_with_feedback` | `Read,Write,Edit,Glob,Grep,Bash` | Review-feedback fixes resume the implementer in the isolated issue worktree and cannot bypass PR review or merge gates. |
| `address_review_core.py:run_address_fix_session` | `Read,Write,Edit,Glob,Grep,Bash,Task,Skill` | Review-thread fixes run in the isolated issue worktree; `Task`/`Skill` support per-comment sub-agents and skill-advisor routing. |
| `github/fleet_sync/conflict_resolver.py:_run_conflict_agent` | `Read,Write,Edit,Glob,Grep,Bash` | Claude SDK fallback runs only in a temporary per-PR conflict worktree with `permission_mode="dontAsk"`, nonce-fenced prompts, bounded turns, and no agent invocation in `--dry-run`. |

Fleet-sync `--dry-run` is a preview contract: GitHub calls, Git subprocesses, pushes,
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

Every PR opened by the automation pipeline goes through GitHub's normal branch
protection and the `pr-policy` required-check gate
(see [`CLAUDE.md`](CLAUDE.md#pr-policy)) — a human still reviews and merges.

## Configuration / boundaries

- **MCP** (Model Context Protocol): `.mcp.json` at the repo root is the
  project-scoped, version-controlled MCP config. Its `mcpServers` map is empty
  — ecosystem integration runs through plugin marketplaces (Mnemosyne), NATS
  (`hephaestus/nats/`), and HTTP REST (Agamemnon/Hermes), none of which is MCP.
  To add a server, edit `.mcp.json`; see [`docs/mcp.md`](docs/mcp.md).
- The deferred follow-ups for cross-agent abstraction (a formal `AgentProtocol`)
  and for wiring `hephaestus.resilience` into the GitHub call path are tracked
  in issues #468 and #469.
