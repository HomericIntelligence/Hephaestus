# ADR-0019: Provider-neutral Pi parity contract

- Status: Accepted
- Date: 2026-07-29
- Tracks: #2514

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
| Required package | Athena | Native Athena package, pinned source/ref, and canonical skill resources | Block Pi stages requiring Athena skills until discovery proves `advise`, `learn`, and `pr-review`. |
| Required package | Delegation | `npm:pi-subagents@0.37.2` supplies the `subagent` tool | Block roles that require `Agent`/delegation when absent. |
| Required package | Web evidence | `npm:pi-web-access@0.15.0` supplies named web tools | Block roles that require `WebFetch` when absent. |
| Repository dependency | Mnemosyne | Canonical checkout at `~/.agent_brain/knowledge` under Athena's dependency-resolution contract | Never install or model Mnemosyne as a Pi package; resolution or trust failure blocks the skill. |
| Unavailable | Interactive action approval | Pi project trust is not the Claude/Codex approval-policy contract | Normal automation is blocked today; #2518 must reject a request that requires interactive approval instead of silently ignoring it. |
| Unavailable | OS sandbox | Pi has no built-in OS sandbox | Reject Pi for a stage requiring an OS-enforced sandbox unless an external enforcement adapter is verified. |

The pin values are owned by the Pi bootstrap/update policy introduced in #2516.
They are intentionally explicit here so a package update cannot silently widen
the provider's capability surface.

### Scope translation

Pi is given only named tools required by the selected role.  Athena frontmatter
is descriptive; Hephaestus owns the enforcement boundary passed to Pi.

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
| `planning` | Athena `advise` plus planner read/search scope | Requires canonical Mnemosyne resolution and Athena discovery. |
| `plan_review` | Read-only reviewer analysis, planner amendment, and a separate Mnemosyne learning subpath | The reviewer remains read-only. `LEARN_WAIT` is Pi N/A until #2517 proves Athena-equivalent learning semantics and #2518 separately scopes its PR-producing workflow. |
| `implementation` | Isolated worktree and write/edit/shell scope | Delegation is opt-in and preflighted, never ambient. |
| `pr_review` | Read-only review; Athena `pr-review` may require explicitly preflighted skill/delegation/web capabilities | No write, merge, CI mutation, or unrestricted network capability. |
| `merge_wait` | No provider authority to merge; `learn` needs a verified Mnemosyne PR workflow | A successful local edit is not learn evidence. |
| `finished` | No model job | Safe tested N/A; it records terminal state and preserves or cleans worktrees. |
| Console wrappers (`hephaestus-automation-loop`, plan, implement, review, and agent-stage) | Preserve the same stage requirements; wrappers do not invent a second Pi path | Provider selection and preflight are shared. |

### Authentication, configuration, and errors

Pi configuration is operator-local. A valid provider can use Pi's supported
OAuth/API-key flow or local model configuration. The legacy, smoke-only
`is_agent_authenticated("pi")` probe currently reads the default
`~/.pi/agent/models.json` location; it neither honors `PI_CODING_AGENT_DIR` nor
constitutes automation-admission evidence. #2516 owns environment-aware
configuration discovery, concrete authentication, and security-cleared package
probes. The current operator-only smoke seam does **not** turn
`HEPH_PI_PROVIDER` / `HEPH_PI_MODEL` into Pi's native provider/model selection
arguments. #2518 owns applying the verified selection, role-derived tool grants,
and lifecycle parity. Those later stages must keep prompts and private aliases
out of publishable diagnostics.

Normal Pi automation does not currently execute. After #2516 admits a verified
package/configuration inventory, #2518 must apply Pi's non-interactive JSON
mode, reject unsupported approval requests, bind resume to a verified
worktree-local session identity, and fail on malformed events or absent session
headers. A timeout, unavailable tool, missing command, malformed event, or
absent session identity must be an actionable provider failure; it must never
downgrade to a Claude/Codex fallback mid-stage. The current read-only operator
smoke seam is transport/redaction evidence only, not evidence of those
post-admission contracts.

### Evidence boundary

`advise`, `learn`, and `pr-review` preserve Athena semantics rather than being
prompt-text aliases.  The mandatory Mnemosyne checkout, source corpus, trust
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
  selection preflight; it cannot advance until #2515 has an accepted,
  security-cleared artifact.
- #2517 replaces legacy Mnemosyne handling with Athena-equivalent semantics.
- #2518 implements the tool-scope translation, explicit approval rejection,
  worktree-local session/resume lifecycle, and complete stage coverage described
  here.
- #2519 provides live conformance evidence; #2520 owns CI, compatibility, and
  operator rollout documentation.

Until those dependencies are verified, Pi remains an explicitly bounded
provider rather than a claimed substitute for every Claude/Codex workflow.
