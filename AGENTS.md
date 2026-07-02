# AGENTS.md

This file is a single-page map of the AI-agent topology and conventions used by
ProjectHephaestus and the wider HomericIntelligence ecosystem. For project-specific
rules (commit policy, branch naming, version model) see [`CLAUDE.md`](CLAUDE.md);
for the catalog of skills the agents invoke, see the [`skills/`](skills/) directory.

## Agents the codebase orchestrates

The `hephaestus.automation` subpackage drives a 3-stage issue/PR pipeline
(plan → implement → drive-green). Each stage runs an external coding agent —
either **Claude Code** or **Codex** — chosen per invocation via the optional
`--agent` CLI flag, or auto-detected with a Claude preference when omitted
(see `hephaestus.agents.runtime.add_agent_argument`). Plan review and
PR-review/address-review are no longer standalone stages: the planner owns its
review loop and the implementer absorbs PR-review + thread-addressing in-loop.

| Stage | Module | Console script | Purpose |
|-------|--------|----------------|---------|
| Plan | `hephaestus.automation.planner` | `hephaestus-plan-issues` | Produce an implementation plan for an open issue |
| ↳ plan review (in-loop) | `hephaestus.automation.plan_reviewer` (within planner loop) | (internal) | Strict R0/R1/R2 review of the plan |
| Implement | `hephaestus.automation.implementer` | `hephaestus-implement-issues` | Carry out the plan in an isolated git worktree |
| ↳ implementation review (in-loop) | `hephaestus.automation._review_utils` (within implementer loop) | (internal) | Strict review of the resulting diff |
| ↳ PR review (in-loop) | `hephaestus.automation.pr_reviewer` (within implementer loop) | (internal) | Inline review of the open PR, policy-aware |
| ↳ address review (in-loop) | `hephaestus.automation.address_review` (within implementer loop) | (internal) | Resolve outstanding inline-review threads |
| Drive green | `hephaestus.automation.ci_driver` | `hephaestus-merge-prs` | Poll CI, fix failing required checks, enable auto-merge once green |

`hephaestus.automation.pr_reviewer` is also exposed as the standalone
`hephaestus-review-prs` console script for manual, out-of-band PR review.

A single one-off stage can be invoked manually via
`hephaestus-agent-stage` (`hephaestus.automation.agent_stage`).

## Agent runtime

`hephaestus.agents.runtime` is the thin layer that abstracts over Claude Code and
Codex. It provides:

- `add_agent_argument(parser)` — adds a uniform `--agent` flag to any CLI.
- `is_codex(agent_str)` — branches between the two providers.
- `run_codex_text(...)`, `run_codex_session(...)`, `resume_codex_session(...)` —
  invoke Codex.
- Claude is invoked via `hephaestus.automation.claude_invoke.invoke_claude_with_session`.

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
| `pr_reviewer.py:run_pr_review_analysis` | `Read,Glob,Grep` | Worktree PR analysis; no write tools; review posting is handled outside the agent call. |
| `_implement_phase.py:ImplementPhase._run_claude_impl_session` | `Read,Write,Edit,Glob,Grep,Bash` | Initial implementation runs in the isolated issue worktree and remains subject to review, CI, and branch protection. |
| `_review_phase.py:ReviewPhase._resume_impl_with_feedback` | `Read,Write,Edit,Glob,Grep,Bash` | Review-feedback fixes resume the implementer in the isolated issue worktree and cannot bypass PR review or merge gates. |
| `address_review.py:run_address_fix_session` | `Read,Write,Edit,Glob,Grep,Bash,Task,Skill` | Review-thread fixes run in the isolated issue worktree; `Task`/`Skill` support per-comment sub-agents and skill-advisor routing. |

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
- `.claude-plugin/` ships the marketplace manifests (the project itself is a
  Claude Code plugin); see also [`docs/plugin-installation.md`](docs/plugin-installation.md).
- **MCP** (Model Context Protocol): `.mcp.json` at the repo root is the
  project-scoped, version-controlled MCP config. Its `mcpServers` map is empty
  — ecosystem integration runs through plugin marketplaces (Mnemosyne), NATS
  (`hephaestus/nats/`), and HTTP REST (Agamemnon/Hermes), none of which is MCP.
  To add a server, edit `.mcp.json`; see [`docs/mcp.md`](docs/mcp.md).
- The deferred follow-ups for cross-agent abstraction (a formal `AgentProtocol`)
  and for wiring `hephaestus.resilience` into the GitHub call path are tracked
  in issues #468 and #469.
