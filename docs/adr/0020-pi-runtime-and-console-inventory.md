# ADR-0020: Exhaustive Pi runtime and console inventory

- Status: Accepted
- Date: 2026-07-29
- Tracks: #2514
- Extends: ADR-0019
- Supersession note: ADR-0025 replaces all rows in this ADR that required Pi
  discovery or preflight for host-owned Athena `advise` or `learn`. Only agent
  jobs that execute through Pi use Pi validation.

## Context

ADR-0019 defines the Pi capability taxonomy and the fail-closed compatibility
boundary. Issue #2514 additionally requires a reviewed inventory of every
current automation stage, direct runtime entry point, and console command.
The accepted ADR is immutable, so this record supplies that exhaustive
inventory rather than amending it.

## Decision

### Direct runtime boundary

Only the shared runtime adapter may select a direct provider. The rows below
are a reviewed source audit of every current non-adapter caller. The static
contract test independently discovers `resolve_agent`, `direct_agent_model`,
`uses_direct_agent_runner`, `run_agent_text`, `run_agent_session`,
`resume_agent_session`, `session_agent_matches`, and
`agent_supports_model_reasoning_effort` callers to enforce the
provider-neutral branch/direct-adapter guard; it does not mechanically
reconcile this prose matrix. Future call-site changes therefore require an
explicit inventory review or a superseding ADR. Except for an explicit N/A row,
the matrix states the required future contract: normal `resolve_agent` selection
blocks Pi until #2516 and #2518 deliver their prerequisites.

| Runtime entry point | Current non-adapter callers | Pi contract |
| --- | --- | --- |
| `resolve_agent` | `automation/agent_stage.py`, `automation/audit_reviewer.py`, `automation/ci_driver.py`, `automation/implementer.py`, `automation/loop_runner.py`, `automation/plan_reviewer.py`, `automation/planner.py`, `automation/pr_reviewer.py`, `automation/pipeline/worker_pool.py`, `github/fleet_sync/cli.py`, `github/tidy.py` | Resolve once at the neutral boundary. Pi fails closed for normal automation until #2516 verifies its package/capability inventory and #2518 enforces lifecycle and tool scopes; it never falls back to another provider. |
| `direct_agent_model` | `automation/_implement_phase.py`, `automation/audit_reviewer.py`, `automation/comment_difficulty.py`, `automation/follow_up.py`, `automation/learn.py`, `automation/plan_reviewer.py`, `automation/post_merge_processor.py`, `automation/pr_manager.py`, `automation/pr_review_core.py`, `github/tidy.py` | Select a model only through the shared adapter. Normal Pi automation remains blocked; #2516 owns environment-aware configuration discovery and preflight, while #2518 owns applying a verified provider/model selection through Pi's native invocation and redacting values. |
| `agent_supports_model_reasoning_effort` | `automation/pipeline/stages/base.py` | Keep provider-specific reasoning-selector syntax in the shared runtime adapter. Pi receives no Codex-style suffix; normal Pi automation remains fail-closed. |
| `uses_direct_agent_runner` | `automation/_implement_phase.py`, `automation/agent_stage.py`, `automation/audit_reviewer.py`, `automation/comment_difficulty.py`, `automation/follow_up.py`, `automation/learn.py`, `automation/plan_reviewer.py`, `automation/post_merge_processor.py`, `automation/pr_manager.py`, `automation/pr_review_core.py`, `automation/prompts/advise.py`, `github/fleet_sync/conflict_resolver.py`, `github/tidy.py` | Choose the shared direct-runner behavior without testing a provider name. Fleet conflict resolution is a safe Pi N/A until #2518 because it rejects direct runners today. |
| `run_agent_text` | `automation/_implement_phase.py`, `automation/audit_reviewer.py`, `automation/comment_difficulty.py`, `automation/plan_reviewer.py`, `automation/pr_manager.py`, `automation/pr_review_core.py`, `github/tidy.py` | Post-admission native non-interactive Pi invocation with the role-derived tool grant. The caller may not branch on `codex` or `pi`. |
| `run_agent_session` | `automation/_implement_phase.py`, `automation/agent_stage.py`, `automation/pipeline/worker_pool.py`, `automation/post_merge_processor.py` | After #2518, preserve cwd, timeout, output, and a validated opaque session identity through the shared adapter. Managed process tracking is a required boundary. |
| `resume_agent_session` | `automation/follow_up.py`, `automation/learn.py`, `automation/pipeline/worker_pool.py` | After #2518, resume only a persisted, verified worktree-local identity; a missing, malformed, or cross-worktree identity is a provider error, not a new/forked session. |
| `session_agent_matches` | `automation/_review_utils.py`, `automation/follow_up.py`, `automation/learn.py` | Keep a persisted session provider bound to its selected agent; #2518 owns fail-closed Pi session-identity enforcement. |
| `run_pi_smoke_session` operator exception | `scripts/pi_smoke.py` | Explicit local, fixed tool-free, non-interactive, no-session/no-approval/no-context/no-discovery adapter smoke only. It has no queue, GitHub, worktree, or merge authority and is not evidence that Pi is admitted to automation. The default Slurm template submits this same smoke command; an operator-supplied `pi_smoke_slurm.py --template` is outside this conformance boundary. |

### Pipeline model resolution

`pipeline.stages.base.stage_model` resolves both `AgentJob.model` and
`CompactJob.model` values for model-driven pipeline work. It is a pipeline
configuration boundary, not a second direct-provider adapter: it asks the
shared runtime whether an agent accepts a reasoning-effort suffix, so only
Codex receives one and Pi model values remain unchanged. Its callers are the
model-driven paths in `planning`, `plan_review`,
`implementation`, `pr_review`, and `merge_wait`; `pr_review` also creates
`CompactJob` instances that compact persisted reviewer/writer sessions. `repo`
and `finished` have no model job. Pi remains fail-closed at the generic runner
until #2516 verifies configuration and packages, then #2518 applies verified
native selection, scope, and compact/resume session handling.

### Queue pipeline stages

The pipeline has exactly these stage names. When the listed prerequisites are
complete, Pi may be admitted at that boundary; until then the stage remains
fail-closed rather than receiving a provider-specific bypass.

| Stage | Pi contract or safe N/A boundary | Owning delivery |
| --- | --- | --- |
| `repo` | No model job; repository and GitHub discovery are provider N/A. | Existing behavior |
| `planning` | Host-owned Athena `advise` needs no harness. The planner agent job uses read/search scope and its selected provider contract. | #2518 owns agent scope/lifecycle |
| `plan_review` | Host-owned `learn` needs no harness. Reviewer and amendment agent jobs use their selected provider contracts. | #2518 owns agent scope/lifecycle |
| `implementation` | Host-owned Athena `advise` needs no harness. Implementation agent work uses an isolated worktree with explicit write/edit/shell tools. Delegation is opt-in. | #2518 owns agent scope/lifecycle |
| `pr_review` | Reviewer/validation work is read-only and Athena `pr-review`, delegation, and web capabilities require verified preflight. Address work runs an implementation role in an isolated worktree with write/edit/shell scope, then the host performs a verified commit/push; Pi has no merge authority. | #2515–#2518 |
| `merge_wait` | No provider authority to merge. Host-owned `learn` needs verified Mnemosyne PR evidence and no harness. | #2517 |
| `finished` | No model job; terminal-state recording and worktree handling are provider N/A. | Existing behavior |

### Console entry points

The following model-driven commands use the same future runtime boundary and
stage contract above; no command may introduce a second Pi path.

| Command | Pi contract |
| --- | --- |
| `hephaestus-automation-loop` | Dispatches the full `repo` → `planning` → `plan_review` → `implementation` → `pr_review` → `merge_wait` → `finished` matrix, or an explicitly selected contiguous subset. |
| `hephaestus-plan-issues` | Runs `planning` plus `plan_review`, including their advise, reviewer/amendment, and learning subpaths. |
| `hephaestus-implement-issues` | Runs `implementation`, `pr_review`, and `merge_wait`, inheriting implementation advice/worktree, review/address/push, and learning boundaries. |
| `hephaestus-review-prs` | Runs `pr_review` only: review/validation is read-only, while its bounded address/push lifecycle can use an isolated write-capable worktree; it never arms merge. |
| `hephaestus-audit-prs` | Read-only direct review scope. |
| `hephaestus-drive-prs-green` | Runs `pr_review` plus `merge_wait`; it inherits the same review/address/push and no-provider-merge boundaries, not a CI-fix session. |
| `hephaestus-agent-stage` | Requested queue-stage contract; no standalone provider fork. |
| `hephaestus-fleet-sync` | Safe N/A for Pi until #2518: its conflict resolver currently rejects direct-runtime providers rather than using a scoped shared adapter. |
| `hephaestus-tidy` | Shared direct adapter with the cleanup role's explicit grant. |
| `python scripts/pi_smoke.py` | Explicit operator-only tool-free, non-interactive, ephemeral adapter smoke; it is not normal automation admission. |
| `python scripts/pi_smoke_slurm.py` / `sbatch scripts/slurm/pi_smoke.sbatch` | The default wrapper and template submit the operator-only smoke seam with a fixed environment export. The wrapper creates a fresh ACL-verified owner-only run directory for scheduler artifacts; direct-template scheduler output is suppressed and Pi writes only its private diagnostic artifact. Neither path has queue or merge authority. An explicit `--template` is an operator-controlled scheduler submission and is not smoke or Pi-admission evidence. |

The remaining `project.scripts` entries do not invoke an agent and are safe
provider N/A boundaries:

- `hephaestus-ensure-state-labels`, `hephaestus-gh`,
  `hephaestus-merge-prs`, `hephaestus-label-severity`,
  `hephaestus-github-stats`, `hephaestus-agent-stats`
- `hephaestus-system-info`, `hephaestus-download-dataset`,
  `hephaestus-coredump-handler`, `hephaestus-run-under-gdb`
- `hephaestus-check-python-version`, `hephaestus-check-test-structure`,
  `hephaestus-check-coverage`, `hephaestus-check-complexity`,
  `hephaestus-filter-audit`, `hephaestus-validate-schemas`,
  `hephaestus-validate-links`, `hephaestus-check-readmes`,
  `hephaestus-check-type-aliases`, `hephaestus-check-docstrings`,
  `hephaestus-check-tier-labels`, `hephaestus-fix-markdown`,
  `hephaestus-audit-doc-policy`, `hephaestus-check-version-consistency`,
  `hephaestus-check-package-versions`, `hephaestus-bump-version`,
  `hephaestus-check-doc-config`, `hephaestus-check-stale-scripts`,
  `hephaestus-mypy-each-file`, `hephaestus-check-links`,
  `hephaestus-validate-anchors`
- `hephaestus-scaffold-subpackage`, `hephaestus-bench-precommit`,
  `hephaestus-check-workflow-inventory`,
  `hephaestus-validate-workflow-checkout`,
  `hephaestus-validate-agents`, `hephaestus-check-cli-tier-docs`,
  `hephaestus-check-api-table-docs`, `hephaestus-check-api-reference`,
  `hephaestus-check-unlinked-todo`

### Session, configuration, and failure contract

| Concern | Required boundary | Evidence or N/A rationale |
| --- | --- | --- |
| Session identity | Post-admission Pi JSON `session.id` must be opaque, valid, and returned through `AgentRunResult`. | Direct-runtime positive-path parsing/capture tests exist. Normal automation remains blocked; #2518 adds stage-level rejection for malformed or absent identity. |
| Smoke session identity | The explicit `--no-session` smoke may receive an ephemeral session event, but must discard it and never emit it in diagnostics. | Runtime and smoke-script regression tests cover both emitted and absent session events. |
| Resume | A post-admission resume must supply a persisted, worktree-local identity; malformed, absent, or cross-worktree identities are provider errors. | The direct-runtime test proves native resume argv construction. #2518 owns non-interactive, fail-closed identity validation and managed-session behavior. |
| Model and configuration | Provider/model selection must be explicit, private aliases redacted, and prompts kept out of argv. | #2516 owns operator config/preflight from an explicit `--pi-dir`; #2518 owns applying verified provider/model selection to Pi's native invocation. Normal automation is fail-closed until both boundaries exist. |
| Authentication | A compliant Pi stage requires a successful #2516 preflight, not a particular private model file. | Normal `resolve_agent` selection fails closed until #2516 and #2518 establish an authenticated config from typed Pi paths, an immutable package/capability inventory, and lifecycle/tool-scope enforcement. |
| Timeout and process lifecycle | Timeout and output must be bounded and redacted. Process-group tracking is required for managed pipeline use. | Pi timeout/redaction tests cover direct runtime behavior; #2518 owns tracked-process parity. |
| Tool, command, or event failure | Post-admission missing package/tool, non-zero command, malformed event, or absent session identity must be actionable and cannot fall back to Claude or Codex mid-stage. | ADR-0019 defines the required fail-closed contract; #2516 and #2518 add preflight and stage-level executable coverage. |

## Alternatives considered

- **Rely only on a hand-maintained direct-caller enforcement list.** Rejected
  because new session or resume callers could silently evade the
  provider-neutral branch guard. The ADR prose remains a reviewed point-in-time
  inventory, while the static guard discovers production callers.
- **Treat unlisted console commands as implicitly N/A.** Rejected because
  provider boundaries must remain reviewable when commands are added.
- **Rewrite ADR-0019.** Rejected because accepted ADRs are immutable
  historical records.

## Consequences

- A new direct runtime caller is automatically subject to the
  provider-specific-branch guard. Direct provider adapters are confined to
  `hephaestus.agents.runtime` and the narrowly scoped smoke exception above.
- #2515–#2518 must satisfy the exact stage and command boundaries above before
  Pi is claimed as a supported pipeline provider.
- A newly added console command requires an explicit provider contract or N/A
  rationale in a superseding ADR.
