# Required status checks

This document records the CI contract and the last verified `main` protection
and ruleset configuration. GitHub policy can change outside git; audit live
state before relying on this record for a merge decision.

## CI is not automation-loop authorization

GitHub Actions validates repository code independently. Normal
`$athena:pr-review` may collect current check evidence as audit context, but
its prose, grades, and decision-shaped output do not authorize the loop. The
automation loop does not change checks, workflows, statuses, artifacts, leases,
or `pull_request_target` events. `pr_review` applies
`state:implementation-go` when no review thread is unresolved. `merge_wait` then
arms native auto-merge once. GitHub applies branch protection, the required
checks below, and any merge queue, and it merges when they pass. Hephaestus
does not read check runs or commit statuses and does not compare heads.

## Queue pre-PR source checks

Developer pre-commit hooks and required PR lint run the shared fast pytest
selection. Before creating a PR, run each new or changed test and verify
collection and success. Use `--override-ini="addopts="` for focused tests outside
the fast selection. Nightly CI owns full unit coverage and the remaining
functional tests, as specified in [ADR-0049](../adr/0049-fast-pr-nightly-tests.md).

Before publishing a Hephaestus implementation, the queue runs the fixed command
`bash scripts/run_ci_local.sh all --rebuild`. Rebuilding the
CI image prevents a prior checkout's dependency environment from weakening the
gate. Each invocation builds from an explicit allowlisted context, captures its
own immutable image ID, and runs every container step against that ID; parallel
workers therefore cannot retag one another's dependencies, and ignored local
credentials, Git metadata, and unrelated artifacts never enter the build
context. The command runs once after each implementation or test-fix turn and
immediately before commit, push, and PR creation. A passing run advances
directly to publication; a failing run returns the item to the implementer and
must pass on the next attempt before publication. For linked implementation
worktrees, the runner mounts the shared Git
metadata read-only so hatch-vcs and Git-aware checks operate on the candidate
commit. Before lint and secret scanning, it also builds an alternate Git index
and read-only candidate tree from `HEAD` plus every non-ignored working-tree
change. This includes untracked source bytes without mutating the implementer's
real index; pre-commit reads the alternate index, while Gitleaks scans both Git
history and that exact candidate tree. The entry point mirrors the locally
executable pull-request jobs in `_required.yml`, including lint, security
scans, schema and version checks, license policy, shell checks, and repository
structure checks. The lint job runs the shared fast test hook. Nightly CI runs
full tests, package checks, and coverage checks. A failure returns to the bounded implementation
test-fix loop instead of publishing a knowingly red branch.

The host must supply `python3` on `PATH`. The nightly artifact job provisions
the fixed Codex artifact and checks the decompressor before it makes a network
request.

For each platform, the shell reports an approved runner-initialization failure.
Approved failures are an absent engine, an unavailable engine, and a failed
no-op container-start probe. The shell exits with code 75 and writes one exact
terminal protocol record. The queue validates the record. On macOS, the queue
runs `uv run pytest tests -q --tb=short`. On other platforms, the queue stops
with `pre_pr_runner_unavailable`. A failure in native verification still
returns the item to the bounded test-fix loop.

The stage puts only the fixed command and source revision in the build job.
The closed worker resolves the system executables. Its launcher reads the
runner and its sourced helper through no-follow directory descriptors. It
compares both files with the immutable implementation-source tree. It executes
anonymous snapshots of the verified bytes. A path rename, symlink change, or
candidate marker cannot grant native-fallback authority.

This queue-specific pass is not a developer pre-commit hook. It cannot run
checks whose inputs do not exist until GitHub creates the PR. `pr-policy` still
validates the live PR body, title, commit subjects, and DCO trailers in Actions.
The direct ruleset contexts and `required-checks-gate` remain authoritative
merge requirements. The local run is early failure feedback only; it does not grant
`state:implementation-go` and does not replace GitHub's exact-head checks.

## Delegated local verification

Use `just ci-check-only` for the local PR/static checks, or
`just ci-lint-check-only` for the lint subset. Prepare the CI image for the
candidate separately before a test-only agent runs these commands. A missing
image fails verification instead of starting an implicit build. The full
`ci-check-only` selection also requires the separate Gitleaks image and disables
pulls for both scans. The lint selection does not run the Gitleaks scans.
The CI builder and runtime use the same pinned Node/npm prefix so the prepared
Markdown hook has the same environment name and executable in both stages.
Rebuild the candidate image after a toolchain or hook-preparation change.
Combining `--check-only` with `--rebuild` fails before engine preparation.
Every CI-image call in these selections uses the prepared Python environment
without syncing dependencies, imports source from the mounted candidate, and
mounts the original checkout read-only. The selected mode uses the existing
container runner and substitutes
[`hephaestus.ci.check_only`](../../hephaestus/ci/check_only.py) for the mutating
pre-commit command. It cannot request the queue's native fallback. The queue
continues to use its existing fixed `all --rebuild` command.

The check-only validator reads the configured pre-commit-stage hooks and their
pinned cached manifests. It uses pre-commit's file and type selectors, including
whole-directory commands and always-run hooks. It does not honor `SKIP` as a
way to omit required checks. Unknown execution contracts or missing prepared
tools fail the command. It does not clone hook repositories or install or
refresh their environments.

Checks run against a private copy of the candidate and its Git index, including
nonignored untracked source and the local private denylist. Native check flags
are used where available. A formatter without a reliable check mode operates
only on that private copy. A source, mode, or index change fails the check, and
the private candidate is restored before the next hook. Markdown configuration
that enables fixes cannot cause a passing result after a source change. Hook
cache paths remain inputs; temporary tool caches belong to the private run. The
Gitleaks history scan also mounts the original checkout read-only.

A pass supplies the current PR/static selection, including the fast pytest
hook. It supplies focused evidence only for the applicable tests that it
collected and passed on the final source. After the signed rebase and clean-head
checks, a test-only agent must cover the affected behavior and each new or
changed test under [AGENTS](../../AGENTS.md) and
[ADR-0053](../adr/0053-focused-local-ci-full-validation.md). Use locked focused
commands and confirm nonempty collection and success. Documentation-only
changes use applicable documentation checks. A complete local suite is not
required before PR creation or merge.

Keep the recorded head and source unchanged across focused checks. Source
review does not execute tests or require these optional local image commands.
Full unit coverage,
installed CLI and package checks, artifact tests, Pi checks, and cross-platform
nightly jobs retain their existing owners. Contract, performance, live Pyxis,
and GitHub PR-policy evidence remain separate when applicable. Local success
does not replace exact-head GitHub CI or Athena PR review.

## macOS PR-review Git boundary

Before candidate code starts, host verification resolves Git with the system
search path from `os.defpath`. It runs that exact Git with `--exec-path` from
the filesystem root. The probe has a fixed timeout, bounded output, and a
Git-only environment. The environment removes `GIT_EXEC_PATH` and
`DEVELOPER_DIR`.

The worker accepts only the canonical `usr/libexec/git-core` directory in one
of these development-tool roots:

- `/Library/Developer/CommandLineTools`
- `/Applications/Xcode.app/Contents/Developer`

The Git executable, each path component, the final directory, and the fixed
`usr/share/git-core/gitconfig` file must exist. They must not be symbolic links
or permit group or world writes. The final support path must be a directory.
The system configuration must be a regular file. A relative path, a path with
control characters, multiple output lines, a different directory, or a path
outside an approved root causes a failure.

The worker derives and validates the direct `usr/bin/git` executable in the
same toolchain. It binds a generated verified-runner command to this executable
before the sandbox starts. This avoids the `/usr/bin/git` developer-selection
shim, which cannot resolve a toolchain through the narrow sandbox profile.

The sandbox grants read access to the direct Git executable, the validated
`git-core` directory, and the exact validated system configuration file. It
grants metadata access to the directory ancestors that macOS needs for path
resolution. It does not grant read access to the system configuration
directory, `/Library`, `/Applications`, or an entire development-tool root.
The candidate environment does not contain `GIT_EXEC_PATH`. A discovery or
validation error stops host verification before candidate code starts.

The host must have the project virtual environment, `sandbox-exec`, system Git
in an approved toolchain, and usable HFS+ quota mounts. A missing prerequisite
must fail the macOS acceptance run. A platform skip is not acceptance evidence.

The focused resolver, profile, and immutable-boundary regressions that this
section used to name were removed with the non-core-library test re-scope. The
`hephaestus/automation` product layer now sits outside the unit-test suite, so
no automated command exercises this boundary. The worker owns the resolver and
the sandbox profile in
[`worker_pool.py`](../../hephaestus/automation/pipeline/worker_pool.py). A
reviewer must confirm a change by reading that module, and must report a macOS
boundary regression that no test can catch.

If a released resolver or profile rule causes a macOS boundary regression,
revert the resolver, its exact profile rule, and this section in one revert
commit. Report the regression on the pull request, because no host-verification
protection test remains. Do not add broader sandbox permissions as a rollback
action.

## Linux PR-review host verification

Linux PR-review host verification runs the candidate command in a read-only
Pyxis/Enroot container. The host mounts the candidate source and Git metadata
read-only. It mounts scratch and Pi logs from an owner-private filesystem whose
total capacity is not more than 1 GiB. Slurm enforces the CPU, memory, process,
file-size, and wall-clock limits. The container uses an isolated network namespace and
receives a scrubbed offline environment. Home, temporary files, and tool
caches use the disposable scratch paths. The coverage runner preserves the
validated `COVERAGE_FILE` path in external disposable scratch.

The worker checks `srun --help` for the standard image, read-only, home-mount,
work-directory, and mount options before it stages the image. It does not
require `--container-unshare`. A missing option or a failed capability check
stops validation.

The verified image supplies `unshare` and `setpriv` from `util-linux`.
After Pyxis starts the container, `unshare` creates new user, network, IPC,
and UTS namespaces. It maps only the current host user to namespace root.
Then `setpriv` sets `no_new_privs` and clears the bounding, inheritable,
and ambient capability sets before it starts the candidate command.
The candidate process has no permitted or effective capabilities after exec.
A missing tool or a failed setup command stops execution. There is no retry
without isolation. The host kernel must permit nested user namespaces.
Rebuild the verified image to supply these tools.
See the [unshare manual](https://man7.org/linux/man-pages/man1/unshare.1.html)
and [setpriv manual](https://man7.org/linux/man-pages/man1/setpriv.1.html).
These setup checks do not replace live acceptance evidence.

Linux applies a process limit of 64 for the real user ID. This limit includes
other processes and threads for that user on the submission and execution
hosts. Use a dedicated verification account with sufficient process capacity.
A busy account can fail to launch a check. The worker keeps the limit in force
and reports the failure; it does not remove the limit to retry.

The image is a host-owned toolchain for isolated verification. Normal CI jobs
and automation runs do not build it. The preparation command stores it at
`~/.agent_brain/automation/host-verification/hephaestus-ci.sqsh`, with a
separate `hephaestus-ci.authority.json` file in the same directory.

The command reuses these files across checkouts and runs after it verifies
the saved digest and authority. Reuse does not need Git, Podman, Docker, or
Enroot. A new candidate commit does not invalidate the host toolchain. Its
authority keeps the original build revision. To update the toolchain, run
`uv run python scripts/prepare_host_verification_pyxis_image.py --rebuild`.
An invalid saved artifact causes a failure; it is not silently rebuilt.
The global directory and its ancestors must not permit other users to write.

On first preparation or an explicit rebuild, the command builds an exact committed Git tree. It exports the
immutable local OCI image ID, not a mutable tag. It writes an owner-read-only
squashfs and a separate owner-read-only authority file on a filesystem that is
visible at the same absolute path on each Slurm compute node:

```bash
just host-verification-pyxis-image
```

The command prints the image path and digest. Supply the image with
`--host-verification-pyxis-image PATH` and a capacity-bounded shared
filesystem with `--host-verification-pyxis-quota-root PATH`. The worker loads
the image. It puts the image, immutable source, and Git metadata
in one private directory beside the source image. It executes only the copied
image. It does not mount a host virtual environment.

The Pyxis boundary is optional for local lanes. When the image and the writable quota root are not both configured — the
Linux rule `select_quota_backend` applies — a Linux worker runs the fixed
immutable check directly from a disposable archive of the proven commit, with
a scrubbed environment (no inherited credentials), bounded wall time, and a
bounded writable scratch.
Unlike the sandboxed paths, the direct path does not deny network access or
host file access: candidate code executes with the operator's reach, which the
operator accepts by not configuring a backend. This direct behavior covers the
rebase lanes and the repository-validation lane on the operator's own source.
Two lanes execute code from arbitrary pull requests and stay fail closed when
the strict sandbox is not configured: the PR-review host-verification lane,
which also requires a configured Pyxis backend for its durable receipt
contract, and the Comet repository-validation local lane, which on Linux has
no sandbox without Pyxis. A configured rear-end fails closed: a missing or
invalid image, authority, runtime, or bounded filesystem stops the
verification. macOS continues to use its native sandbox boundary. Other
platforms remain fail-closed until a reviewed isolation backend exists.

The harness constructs a validated `PyxisExecutionPlacement` and passes it to
`WorkerPool(host_verification_pyxis_placement=...)`. Container commands use
explicit Slurm allocation and node selectors. The normal worker leaves
placement to Slurm when this optional setting is absent. See the
[Slurm srun options](https://slurm.schedmd.com/srun.html).

## Current required contexts

Classic branch protection requires:

| Required context | Source |
|------------------|--------|
| `required-checks-gate` | `.github/workflows/_required.yml` |

The active ruleset requires these direct contexts:

| Required context | Source |
|------------------|--------|
| `lint` | `.github/workflows/_required.yml` |
| `security/dependency-scan` | `.github/workflows/_required.yml` |
| `security/secrets-scan` | `.github/workflows/_required.yml` |
| `schema-validation` | `.github/workflows/_required.yml` |
| `deps/version-sync` | `.github/workflows/_required.yml` |
| `pr-policy` | `.github/workflows/_required.yml` |

The active `homeric-main-baseline` branch ruleset also applies
`required_signatures` to `main`. Signature enforcement is repository policy,
not a duplicate GitHub Actions context.

## Merge queue execution

The `_required.yml` workflow runs on `merge_group: checks_requested`. GitHub
therefore evaluates the required pull-request suite against each synthetic
`gh-readonly-queue/...` commit, including the aggregate
`required-checks-gate` and every direct ruleset context. Because the live queue's
`HEADGREEN` grouping strategy evaluates the synthetic group head, the
merge-group `pr-policy` job binds the queue ref to the live GraphQL merge-queue
entry and exact synthetic group head, enumerates every source PR represented by
that head, and requires a successful GitHub-Actions-owned `pr-policy` check on
each PR's exact current head SHA. Both queue entries and check runs are fully
paginated and cardinality-checked. Missing, malformed, stale, pending, failed,
or incomplete source-PR evidence fails closed.

## Maintenance

- **Owner:** The `.github/` owner in
  [CODEOWNERS](../../.github/CODEOWNERS).
- **Versioned source:** Maintain this document from the `jobs` mapping in
  [`_required.yml`](../../.github/workflows/_required.yml).
- **External source:** The live branch-protection and ruleset output collected
  by the commands under [Live audit](#live-audit).
- **Trigger:** Reconcile this document when the workflow job or context names
  change, when a branch-protection rule or ruleset changes, and during the
  pre-release review.

## Aggregate workflow coverage

`required-checks-gate` depends on all code-validation jobs in `_required.yml`.
Each dependency must succeed on pull-request and merge-group events. On a push,
only `pr-policy` can skip because no pull request exists. A failed, cancelled,
missing, unknown, or other skipped dependency fails the aggregate. The gate
also rejects a result census that differs from its complete expected-job list.

The unit guard requires exact equality between the top-level workflow jobs,
the aggregate `needs` list, and the runtime expected-job list. It also binds
the single push-only skip to the `pr-policy` condition. When you change the job
graph, update the top-level job, aggregate `needs`, runtime census, applicable
heavy-job set, runtime fixtures, structural tests, and hosted event evidence in
one reviewed change. When you change a supported event, also update the skip
policy and documentation. Add a skip exception only when the job has an exact
event condition and the unit guard enforces it.

The workflow handles code events only. It must not gain label, review, or
auto-merge event triggers. The automation loop handles review labels and
merge-state actions.

The gate fans in the `_required.yml` code-validation jobs: `lint`, `pr-policy`,
the `security/*` scans (including `security/workflow-scan`,
the zizmor GitHub Actions SAST gate added for issue #2151), `license-scan`, and
more. Enumerating each one individually in branch protection is brittle: renaming
a job, adding a job, or splitting one silently changes what's required, and nobody
notices until something slips through.

## Agent contract governance

The `agent-contract` job is the retained governance check from issue #2853.
Each `_required.yml` run on pull-request, merge-group, and exact-main push
calls the released Athena validator. The call goes through the reusable
workflow at the protected signed tag `agent-contract-v1.0.0`. That tag has an
immutable GitHub release, so the called validator content cannot change. The
job is a member of the aggregate fan-in only. It is not a direct required
context in the tables above.

The local CI container cannot run an external reusable workflow. The focused
tests in `tests/unit/docs/test_agent_contract.py`,
`tests/unit/ci/test_agent_contract_inventory.py`, and
`tests/unit/ci/test_merge_queue_required_checks.py` are wiring checks. The
GitHub `agent-contract` jobs supply the authoritative provider result.

Six additional direct callers run the same read-only job: `auto-tag.yml`
(`auto-tag` needs), `contract.yml` (`github-contract` needs),
`nightly-tests.yml` (`notify-failure` needs), `performance.yml`
(`notify-failure` needs), `security.yml` (`notify-failure` needs), and
`release.yml` (`build-and-publish` needs; `deploy-docs` inherits the check
through that dependency). Issue-event and comment-event workflows carry no
contract job.

## Hosted cache behavior

The required-checks workflow no longer caches the rootless Podman storage
directory (`~/.local/share/containers/storage`). Rootless layer permissions and
namespace-mapped ownership prevent the GitHub Actions cache action from reading
all selected paths, which caused cache save failures after successful validation
steps. The affected cache steps have been removed from both
`_required.yml` and `security.yml`.

The following caches remain active and are known to work correctly:

- **Pre-commit environments** (`~/.cache/pre-commit`) — cached in the `lint` job.
- **Mypy incremental cache** (`.mypy_cache`) — cached in the `lint` job.

Each CI job builds the `hephaestus-ci:local` image from `ci/Containerfile` on
every run. The image build is fast on GitHub-hosted runners because the base
image layers are pre-pulled by the runner environment. A dedicated image or
archive cache may be proposed separately if measured restore/rebuild benefit
justifies its storage and identity controls.

## Live audit

```bash
repo=HomericIntelligence/Hephaestus
branch=main
gh api "repos/$repo/branches/$branch/protection/required_status_checks"
gh api "repos/$repo/rulesets" --paginate
gh ruleset check --default --repo "$repo"
```

When changing required checks, first capture the complete live arrays and app
bindings. Modify only the named code-validation context, then read back the
arrays and prove every unrelated context and binding is unchanged. Do not add
the automation loop's label, a review result, or an internal loop artifact as
a GitHub Actions required check.
