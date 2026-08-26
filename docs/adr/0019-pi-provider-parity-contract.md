# ADR-0019: Provider-neutral Pi parity contract

- Status: Accepted
- Date: 2026-07-29
- Tracks: #2514
- Supersession note: ADR-0025 replaces every requirement in this ADR that made
  host-owned Athena `advise` or `learn` depend on Pi discovery, package
  inventory, preflight, tool grants, or sessions. Only work that runs an agent
  through Pi uses the Pi admission contract below.

## Context

Hephaestus already dispatches Claude Code, Codex, and Pi through the shared
`hephaestus.agents.runtime` boundary.  Pi is not yet a first-class automation
provider: its base CLI, optional packages, and trust model have materially
different semantics from the other providers.  Treating a Pi tool allowlist as
an operating-system sandbox, or treating Mnemosyne as a Pi package, would hide
those differences and weaken the review loop.

This record defines the compatibility boundary for the staged Pi work.  It is
the parity contract, not a claim that every later requirement is implemented
on the revision that introduces it.

## Decision

### Provider-neutral orchestration

All stage and direct-call orchestration continues to choose a provider through
`AgentName`, `resolve_agent`, `run_agent_text`, `run_agent_session`, and
`resume_agent_session`.  Stages may describe required capabilities, but may
not fork their orchestration by provider to conceal a missing Pi capability.
Provider adapters translate those requirements or fail before an agent is run.

The executable registry in `hephaestus.agents.runtime.AgentCapabilities`
separates Pi base, package-provided, and unavailable capabilities.  Its unit
test is the machine-checkable companion to this record.

### Pi capability classes

| Class | Capability | Pi contract | Failure behavior |
| --- | --- | --- | --- |
| Base CLI | Read, write, shell, search | Built-in `read`, `write`, `edit`, `bash`, `grep`, `find`, and `ls` tools | Do not start when the CLI probe fails. |
| Base CLI | Sessions and resume | JSON event stream supplies an opaque session ID; a post-admission resume must validate a worktree-local identity | Normal automation is blocked today. #2518 must reject malformed events, missing identities, and cross-worktree resume/fork prompts rather than treating them as success. |
| Base CLI | Skills | Pi can load an explicit skill file or directory | Do not claim Athena discovery until the Athena package probe succeeds. |
| Base CLI | Tool allowlist | `--tools` limits model-visible built-in and package tools | Do not call it an OS sandbox. |
| Required package | Athena | Native Athena package, pinned source/ref, and canonical skill resources | Block only Pi agent work that loads an Athena skill, such as `pr-review`, until discovery proves that skill. Host-owned `advise` and `learn` do not use this package gate. |
| Required package | Delegation | `npm:pi-subagents@0.37.2` supplies the `subagent` tool | Block roles that require `Agent`/delegation when absent. |
| Required package | Web evidence | `npm:pi-web-access@0.15.0` supplies named web tools | Block roles that require `WebFetch` when absent. |
| Repository dependency | Mnemosyne | Canonical checkout at `~/.agent_brain/knowledge` under Athena's dependency-resolution contract | Never install or model Mnemosyne as a Pi package; resolution or trust failure blocks the skill. |
| Unavailable | Interactive action approval | Pi project trust is not the Claude/Codex approval-policy contract | Normal automation is blocked today; #2518 must reject a request that requires interactive approval instead of silently ignoring it. |
| Unavailable | OS sandbox | Pi has no built-in OS sandbox | Reject Pi for a stage requiring an OS-enforced sandbox unless an external enforcement adapter is verified. |

The top-level Pi CLI version is exact, but package-internal dependency
resolution is supplied by the installed Pi artifact and its shipped shrinkwrap.
Hephaestus does not yet own a repository npm lockfile or integrity assertion
for that artifact. #2516 owns the bootstrap, update, inventory, and integrity
admission policy; the explicit compatibility pins here prevent a reviewed
package update from silently widening the provider's capability surface.

### Scope translation

Pi is given only named tools required by the selected role.  Athena frontmatter
is descriptive; Hephaestus owns the enforcement boundary passed to Pi.

The pre-admission `run_pi_smoke_session` exception is deliberately narrower
than any stage role: it is non-interactive, ephemeral, tool-free, and disables
project approval/context plus extension, skill, prompt-template, and theme
discovery. It demonstrates transport and diagnostics only; it cannot establish
that a role-derived Pi scope has been admitted.

| Hephaestus capability | Pi grant | Notes |
| --- | --- | --- |
| `Read`, `Grep`, `Glob` | `read`, `grep`, `find`, `ls` | Review baseline; no `bash`, write, package, or web tool. |
| `Bash` | `bash` | Add only to the role that needs command execution. |
| `Write`, `Edit` | `write`, `edit` | Implementation worktree only. |
| `Skill` | Athena skill discovery plus the base skill loader | Requires the Athena package probe. |
| `Agent` | `subagent` | Requires `pi-subagents`; child scope is an intersection with its parent. |
| `WebFetch` | `web_search`, `source_check`, `fetch_content`, `get_search_content` | Requires `pi-web-access`; never ambiently enabled for review. |

### Entry-point and stage matrix

| Surface | Pi role and required contract | Current boundary |
| --- | --- | --- |
| Direct `run_agent_text` callers (`audit_reviewer`, `plan_reviewer`, `pr_review_core`, `tidy`, and related legacy flows) | Native JSON execution, model/provider selection, and a role-derived tool grant | The shared runner rejects Pi before dispatch until its admission prerequisites exist; unsupported grants must fail before invocation. |
| Direct `run_agent_session` and `resume_agent_session` callers (`agent_stage`, implementation, CI-fix, follow-up, learn, and post-merge flows) | Opaque session ID, resume, timeout, process lifecycle, and redacted diagnostics | The shared runner rejects Pi before dispatch until its admission prerequisites exist. Session lifecycle has executable runtime tests; process tracking is completed in #2518. |
| `repo` | No model job | Safe tested N/A; it performs repository and GitHub discovery only. |
| `planning` | Host-owned Athena `advise`, then planner agent work with read/search scope | `advise` needs no harness. Only the planner agent job uses provider admission. |
| `plan_review` | Read-only reviewer analysis, planner amendment, and a separate host-owned Mnemosyne learning subpath | Host learning needs no harness. Reviewer and amendment agent jobs use their selected provider contract. |
| `implementation` | Host-owned Athena `advise`, then implementation work in an isolated worktree with write/edit/shell scope | `advise` needs no harness. Only implementation agent work uses provider admission. Delegation is opt-in, never ambient. |
| `pr_review` | Reviewer/validation work is read-only; Athena `pr-review` may require preflighted skill/delegation/web capabilities. Address work resumes an implementation role in an isolated worktree with write/edit/shell scope. | The host controls commit/push after address work and Pi has no merge or CI authority. Pi is N/A until #2515–#2518 separately preflight both role scopes. |
| `merge_wait` | No provider authority to merge; host-owned `learn` needs a verified Mnemosyne PR workflow | `learn` needs no harness. A successful local edit is not learn evidence. |
| `finished` | No model job | Safe tested N/A; it records terminal state and preserves or cleans worktrees. |
| Console wrappers (`hephaestus-automation-loop`, plan, implement, review, and agent-stage) | Preserve the exact stage requirements recorded in ADR-0020; wrappers do not invent a second Pi path | Provider selection and preflight are shared. |

### Authentication, configuration, and errors

Pi configuration is operator-local. A valid provider can use Pi's supported
OAuth/API-key flow or local model configuration. The smoke-only
`is_agent_authenticated("pi")` probe reads the configured Pi package directory
and does not constitute automation-admission evidence. Provider and model aliases
for the operator smoke seam come from the explicit, owner-only mode-0600 TOML
selected by `--pi-alias-config`; they are never read from Hephaestus environment
variables. #2516 owns concrete authentication and security-cleared package
probes. #2518 owns applying verified selection, role-derived tool grants, and
lifecycle parity. Those later stages must keep prompts and private aliases out
of publishable diagnostics.

Normal Pi automation does not currently execute. After #2516 admits a verified
package/configuration inventory, #2518 must apply Pi's non-interactive JSON
mode, reject unsupported approval requests, bind resume to a verified
worktree-local session identity, and fail on malformed events or absent session
headers. A timeout, unavailable tool, missing command, malformed event, or
absent session identity must be an actionable provider failure; it must never
downgrade to a Claude/Codex fallback mid-stage. The current tool-free operator
smoke seam is transport/redaction evidence only, not evidence of those
post-admission contracts.

### Evidence boundary

`advise`, `learn`, and `pr-review` preserve Athena semantics rather than being
prompt-text aliases. Host-owned `advise` and `learn` do not execute through a
provider. The mandatory Mnemosyne checkout, source corpus, trust
gates, failed-command behavior, and learning-through-PR evidence are governed
by Athena.  Mnemosyne content remains fenced untrusted context and cannot set a
pipeline verdict or widen a tool grant.

## Alternatives considered

- **Treat Pi as Codex with renamed commands.** Rejected: Pi's package, tool,
  approval, authentication, and sandbox semantics are different.
- **Use `--tools` as sandbox evidence.** Rejected: it is a model-tool
  allowlist, not an OS isolation boundary.
- **Bundle Athena, subagents, web access, and Mnemosyne together.** Rejected:
  it obscures trust/update ownership and violates Athena's repository contract.
- **Add stage-specific Pi paths.** Rejected: the common runtime boundary is
  the maintainable extension point; gaps must remain visible and fail closed.

## Consequences

- #2515 publishes and proves the native Athena package.
- #2516 owns package installation, inventory, command capability probes, and
  selection preflight and is blocked by this parity contract (#2514). It may
  implement those checks after parity lands, but normal Pi automation remains
  unadmitted while #2515 awaits an accepted, security-cleared artifact and
  until the later scope and lifecycle gates are complete.
- #2517 replaces legacy Mnemosyne handling with Athena-equivalent semantics.
- #2518 implements the tool-scope translation, explicit approval rejection,
  worktree-local session/resume lifecycle, and complete stage coverage described
  here.
- #2519 provides live conformance evidence; #2520 owns CI, compatibility, and
  operator rollout documentation.

Until those dependencies are verified, Pi remains an explicitly bounded
provider rather than a claimed substitute for every Claude/Codex workflow.
