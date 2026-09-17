# ADR-0054: Comet review validation

- Status: Accepted
- Date: 2026-09-16
- Tracks: #3088

## Context

The PR-review stage previously recognized only the Hephaestus host-validation
profile. A Comet PR could have a verified source checkout and successful CI,
but the stage could not use that evidence. Repeated review attempts could not
correct the unsupported profile.

Source identity, command execution, and source review are separate requirements.
A missing local runtime must not prevent use of complete, valid CI evidence.
CI success must not replace a source review or authorize a merge.

## Decision

Bind the detached source workspace before validation. Select Comet only for
`LLM360/comet`. Keep the existing Hephaestus profile and jobs without repository
validation metadata unchanged. No bootstrap grant is necessary for Comet.

Admit only explicit source profiles. Each profile binds the complete control
inventory, Git modes, sizes, and SHA-256 digests. Unknown files that can change
configuration or replace validation dependencies cause admission to fail.
Do not execute a repository selector to discover commands. The host selects
fixed command vectors from the admitted policy and real Git change records.
Keep the PR target base separate from the merge base used for the diff.

The initial profiles bind Comet source at `d19d3dd` and the historical CI source
at `5232ef5`. They have separate identities. A mixture of their control files
is not a profile. Head and target base must have the same profile. CI must
also prove that profile at its immutable merge witness.

Freeze one plan for each attempt. Bind it to the repository, PR, head, target
base, source workspace, profile, applicable checks, generation, and request
nonce. Store pending request ownership before submission. Consume a callback
before the coordinator changes state. Reject a stale, malformed, duplicate,
or unowned result. Failed evidence remains a gap after later success.
Restart discards attempt evidence and returns through entry and source checks.

### CI evidence

Read only fixed GitHub endpoints within a maximum 120-second deadline and
bounded request, response, page, object, and aggregate limits. Use two complete,
agreeing observations of the PR, run, attempt, jobs, steps, and conclusions.

The run must refer to the exact reviewed head and branch in the same repository.
Its five local reusable-workflow records must identify one immutable merge
commit. That commit must have exactly the ordered parents `[target base, head]`.
Read that immutable commit; do not resolve a mutable pull merge ref.

Read and admit the controls at head, target base, and merge commit. A successful
aggregate job or no-op job cannot replace the required command steps. Retain all
applicable checks. Ordinary PR CI does not cover nightly-only control and viewer
validators. Their absence can leave checks for local execution; it cannot remove
those checks from the plan.

The historical fixture retains run `35159405169`, attempt 1, and its 16 jobs.
Its PR response is an explicit normalization of the retained pre-merge GraphQL
identity and repository objects from that run. It is not an original REST
pull-response capture. The fixture keeps the empty `pull_requests` list and
proves identity through the reusable-workflow merge witness.

### Local evidence

Complete CI coverage proceeds without a local runtime lookup. Otherwise, use
local execution only for uncovered eligible checks. The workflow-contract
validator requires a download and is not eligible for this offline route.
A missing or unsafe runtime produces a gap; the review does not install one.

The host prepares the fixed capability under:

```text
<trusted-hephaestus>/build/hephaestus-review-validation/comet/<uv-lock-sha256>/
```

Admit the complete manifest and file inventory before execution. Require the
bound dependency files, Python 3.12, and uv 0.12.7. Reject symbolic links,
startup hooks, changed hashes, and unsafe ownership or modes. The manifest
limit is 16 MiB. Other limits are 50,000 files, 1 GiB per file, 8 GiB total,
and 4,096 UTF-8 bytes per path. Recheck admission immediately before launch.

Use the existing macOS or Linux isolation backend. Mount source and runtime
read-only; use bounded scratch storage for permitted output. On Linux, the
runtime binding must be on shared storage available to the compute node.
There is no unsandboxed fallback. The host sets reviewed-source `PYTHONPATH`
after the generic environment, and child Python processes inherit it. Use the
sealed tools with offline dependency settings. The strict documentation build
uses a bounded scratch `site` directory.

`BuildTestJob.repository_validation` carries the complete execution identity.
Jobs without this metadata retain their existing behavior. Local receipts must
come from the immutable execution result and match the pending request.

### Review and merge boundaries

CI and local receipts can jointly cover the plan. Require complete current
coverage before a clean evaluation, audit persistence, and implementation GO.
Recheck head and base identities at the final boundaries. Give both reviewer
prompts a separate fenced summary of the current evidence. Preserve each
receipt's evidence kind. Never truncate the summary into an apparent pass.

Validation evidence does not decide source-review findings. Repository merge
policy, required CI, signatures, DCO, and thread resolution remain separate
requirements. This decision grants no production authority.

## Alternatives considered

Treat unsupported execution as a passing skip: rejected because it invents
evidence and permits an incomplete plan.

Require a local runtime when CI already covers the plan: rejected because it
adds a host capability requirement without additional validation evidence.

Interpret arbitrary workflow YAML: rejected because command discovery would
expand execution authority beyond explicitly reviewed source profiles.

## Consequences

A new control version needs an explicit profile update before admission.
Payload changes can use an existing profile when all control identities match.
Missing evidence remains visible and cannot become GO through retry or restart.

The real runtime fixture proves dependency imports and reviewed-source import
precedence in the main and nested Python processes. It does not replace
isolation-backend acceptance evidence. Focused checks remain subject to
[ADR-0053](0053-focused-local-ci-full-validation.md).

Rollback removes Comet admission, clears live attempt state, and restarts from
source admission. Preserve source identity, prior review evidence, drafts, and
unrelated work. No persisted validation-state migration is necessary.
