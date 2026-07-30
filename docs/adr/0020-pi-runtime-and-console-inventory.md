# ADR-0020: Exhaustive Pi runtime and console inventory

- Status: Accepted
- Date: 2026-07-29
- Tracks: #2514
- Extends: ADR-0019

## Context

ADR-0019 defines the Pi capability taxonomy and the fail-closed compatibility
boundary. Issue #2514 additionally requires a reviewed inventory of every
current automation stage, direct runtime entry point, and console command.
The accepted ADR is immutable, so this record supplies that exhaustive
inventory rather than amending it.

## Decision

### Direct runtime boundary

Only the shared runtime adapter may select a direct provider. Every current
non-adapter caller is listed below. The static contract test discovers these
callers from their actual `resolve_agent`, `direct_agent_model`,
`uses_direct_agent_runner`, `run_agent_text`, `run_agent_session`,
`resume_agent_session`, and `session_agent_matches` calls, rather than relying
on a manually maintained file list. Except for an explicit N/A row, the matrix
states the required future contract: normal `resolve_agent` selection blocks
Pi until #2516 and #2518 deliver their prerequisites.

| Runtime entry point | Current non-adapter callers | Pi contract |
| --- | --- | --- |
| `resolve_agent` | `automation/agent_stage.py`, `automation/audit_reviewer.py`, `automation/ci_driver.py`, `automation/implementer.py`, `automation/loop_runner.py`, `automation/plan_reviewer.py`, `automation/planner.py`, `automation/pr_reviewer.py`, `automation/pipeline/worker_pool.py`, `github/fleet_sync/cli.py`, `github/tidy.py` | Resolve once at the neutral boundary. Pi fails closed for normal automation until #2516 verifies its package/capability inventory and #2518 enforces lifecycle and tool scopes; it never falls back to another provider. |
| `direct_agent_model` | `automation/_implement_phase.py`, `automation/audit_reviewer.py`, `automation/ci_fix_flow.py`, `automation/ci_fix_orchestrator.py`, `automation/comment_difficulty.py`, `automation/follow_up.py`, `automation/learn.py`, `automation/plan_reviewer.py`, `automation/post_merge_processor.py`, `automation/pr_manager.py`, `automation/pr_review_core.py`, `github/tidy.py` | Select a model only through the shared adapter. Normal Pi automation remains blocked; #2516 owns environment-aware configuration discovery and preflight, while #2518 owns applying a verified provider/model selection through Pi's native invocation and redacting values. |
| `uses_direct_agent_runner` | `automation/_followup_phase.py`, `automation/_implement_phase.py`, `automation/agent_stage.py`, `automation/audit_reviewer.py`, `automation/ci_fix_flow.py`, `automation/ci_fix_orchestrator.py`, `automation/comment_difficulty.py`, `automation/follow_up.py`, `automation/learn.py`, `automation/plan_reviewer.py`, `automation/post_merge_processor.py`, `automation/pr_manager.py`, `automation/pr_review_core.py`, `automation/prompts/advise.py`, `github/fleet_sync/conflict_resolver.py`, `github/tidy.py` | Choose the shared direct-runner behavior without testing a provider name. Fleet conflict resolution is a safe Pi N/A until #2518 because it rejects direct runners today. |
| `run_agent_text` | `automation/_implement_phase.py`, `automation/audit_reviewer.py`, `automation/comment_difficulty.py`, `automation/plan_reviewer.py`, `automation/pr_manager.py`, `automation/pr_review_core.py`, `github/tidy.py` | Native non-interactive Pi invocation with the role-derived tool grant. The caller may not branch on `codex` or `pi`. |
| `run_agent_session` | `automation/_implement_phase.py`, `automation/agent_stage.py`, `automation/ci_fix_flow.py`, `automation/ci_fix_orchestrator.py`, `automation/pipeline/worker_pool.py`, `automation/post_merge_processor.py` | Preserve cwd, timeout, output, and opaque session identity through the shared adapter. Managed process tracking is a required #2518 boundary. |
| `resume_agent_session` | `automation/ci_fix_orchestrator.py`, `automation/follow_up.py`, `automation/learn.py`, `automation/pipeline/worker_pool.py` | Resume the persisted opaque identity through the shared adapter; a missing or malformed identity is a provider error, not a new session. |
| `session_agent_matches` | `automation/_followup_phase.py`, `automation/_review_utils.py`, `automation/follow_up.py`, `automation/learn.py` | Keep a persisted session provider bound to its selected agent; #2518 owns fail-closed Pi session-identity enforcement. |
| `run_pi_session` operator exception | `scripts/pi_smoke.py` | Explicit local, read-only adapter smoke only. It has no queue, GitHub, worktree, or merge authority and is not evidence that Pi is admitted to automation. The default Slurm template submits this same smoke command; an operator-supplied `pi_smoke_slurm.py --template` is outside this conformance boundary. |

### Pipeline model resolution

`pipeline.stages.base.stage_model` resolves the `AgentJob.model` value for all
model-driven pipeline jobs. It is a pipeline configuration boundary, not a
second direct-provider adapter: it currently adds a reasoning-effort suffix
only for Codex and leaves Pi model values unchanged. Its callers are the
model-driven paths in `planning`, `plan_review`, `implementation`, `pr_review`,
and `merge_wait`; `repo` and `finished` have no model job. Pi remains
fail-closed at the generic runner until #2516 verifies configuration and
packages, then #2518 applies the verified native selection and role scope.

### Queue pipeline stages

The pipeline has exactly these stage names. When the listed prerequisites are
complete, Pi may be admitted at that boundary; until then the stage remains
fail-closed rather than receiving a provider-specific bypass.

| Stage | Pi contract or safe N/A boundary | Owning delivery |
| --- | --- | --- |
| `repo` | No model job; repository and GitHub discovery are provider N/A. | Existing behavior |
| `planning` | Read/search scope plus Athena `advise`; canonical Mnemosyne resolution and package discovery are required. | #2515–#2517 |
| `plan_review` | Read-only review scope; no write, merge, CI mutation, delegation, or web capability. | #2518 |
| `implementation` | Isolated worktree plus explicitly scoped write/edit/shell tools; delegation is opt-in. | #2516, #2518 |
| `pr_review` | Read-only review scope; Athena `pr-review`, delegation, and web capabilities require their own verified preflight. | #2515–#2518 |
| `merge_wait` | No provider authority to merge; `learn` requires verified Mnemosyne PR evidence. | #2517, #2518 |
| `finished` | No model job; terminal-state recording and worktree handling are provider N/A. | Existing behavior |

### Console entry points

The following model-driven commands use the same future runtime boundary and
stage contract above; no command may introduce a second Pi path.

| Command | Pi contract |
| --- | --- |
| `hephaestus-automation-loop` | Dispatches the queue stage matrix. |
| `hephaestus-plan-issues` | Planning read/search and Athena `advise` prerequisites. |
| `hephaestus-implement-issues` | Implementation worktree and scoped write tools. |
| `hephaestus-review-prs` | Read-only PR review scope. |
| `hephaestus-audit-prs` | Read-only direct review scope. |
| `hephaestus-drive-prs-green` | Scoped CI-fix session path. |
| `hephaestus-agent-stage` | Requested queue-stage contract; no standalone provider fork. |
| `hephaestus-fleet-sync` | Safe N/A for Pi until #2518: its conflict resolver currently rejects direct-runtime providers rather than using a scoped shared adapter. |
| `hephaestus-tidy` | Shared direct adapter with the cleanup role's explicit grant. |
| `python scripts/pi_smoke.py` | Explicit operator-only read-only adapter smoke; it is not normal automation admission. |
| `python scripts/pi_smoke_slurm.py` / `sbatch scripts/slurm/pi_smoke.sbatch` | The default template submits the operator-only smoke seam without queue or merge authority. An explicit `--template` is an operator-controlled scheduler submission and is not smoke or Pi-admission evidence. |

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
| Session identity | Pi JSON event `session.id` must be opaque and returned through `AgentRunResult`. | Direct-runtime positive-path parsing/capture tests exist. #2518 adds stage-level enforcement for malformed or absent identity. |
| Resume | A resume must supply a persisted identity; malformed or absent replacement identity is a provider error. | The direct-runtime test proves native resume argv construction. #2518 owns fail-closed identity validation and managed-session behavior. |
| Model and configuration | Provider/model selection must be explicit, private aliases redacted, and prompts kept out of argv. | #2516 owns an operator config/preflight that honors `PI_CODING_AGENT_DIR`; #2518 owns applying the verified provider/model selection to Pi's native invocation. Normal automation is fail-closed until both boundaries exist. |
| Authentication | A compliant Pi stage requires a successful #2516 preflight, not a particular private model file. | Normal `resolve_agent` selection fails closed until #2516 and #2518 establish an authenticated config honoring `PI_CODING_AGENT_DIR`, an immutable package/capability inventory, and lifecycle/tool-scope enforcement. |
| Timeout and process lifecycle | Timeout and output must be bounded and redacted. Process-group tracking is required for managed pipeline use. | Pi timeout/redaction tests cover direct runtime behavior; #2518 owns tracked-process parity. |
| Tool, command, or event failure | Missing package/tool, non-zero command, malformed event, or absent session identity must be actionable and cannot fall back to Claude or Codex mid-stage. | ADR-0019 defines the required fail-closed contract; #2516 and #2518 add preflight and stage-level executable coverage. |

## Alternatives considered

- **Retain a hand-maintained direct-caller list.** Rejected because new session
  or resume callers could silently evade the provider-neutral branch guard.
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
