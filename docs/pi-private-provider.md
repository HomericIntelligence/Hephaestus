# Private Pi Provider Setup

Configure private OpenAI-compatible providers only in the operator-local Pi
configuration, for example under `~/.pi/agent/models.json`. Do not commit Pi
provider config, endpoint URLs, hostnames, checkpoint names, model identifiers,
or operator-local aliases.

Use placeholders in documentation:

- `<operator-local-alias>`
- `<operator-local-provider-alias>`
- `<private-provider-url>`
- `<private-model-name>`

Install the real Pi CLI in the automation environment; do not substitute a fake
`pi` binary for adapter validation:

```bash
npm install -g --ignore-scripts @earendil-works/pi-coding-agent@0.80.2
pi --version
```

Set the local smoke sentinels at runtime:

```bash
export HEPH_PI_PROVIDER=<operator-local-provider-alias>
export HEPH_PI_MODEL=<operator-local-alias>
python3 scripts/pi_smoke.py
```

Both variables are required by the explicit smoke seam. They are not yet
forwarded as Pi native provider/model selection arguments, so a successful
smoke validates only the bounded adapter path, not a requested provider/model
selection. That seam is non-interactive and ephemeral: it disables sessions,
tools, project approval/context, and extension, skill, prompt-template, and
theme discovery. #2516 owns configuration discovery and preflight; #2518 owns
native selection and scoped pipeline admission.

The smoke command starts Pi with a minimized execution, configuration, locale,
and temporary-directory environment. It disconnects Pi from the caller's
standard input, so a piped parent input cannot become part of the fixed smoke
prompt. It does not forward the `HEPH_PI_*` sentinels, arbitrary Pi settings,
GitHub/cloud credentials, or an operator's telemetry preference; it forces
`PI_TELEMETRY=0` and `PI_SKIP_VERSION_CHECK=1`. A generated Pi session ID is
discarded and never printed or written to the diagnostic artifact.

The smoke command writes each artifact below a fresh, owner-only
`pi-smoke-*` run directory under `--log-dir`; it never reuses a caller-supplied
directory for an artifact. Before Pi runs, the wrapper verifies the complete
path chain has no symlinks or replaceable non-sticky ancestor, clears and
verifies the new run directory's access ACLs, and fails closed when it cannot
establish that boundary. Existing log roots must also have no access ACL grant.
This permits safe sticky system roots such as `/tmp` while preserving atomic
creation and ownership verification for the run directory. It loads both
`.heph-project-denylist` and `.heph-private-denylist` from its
working-directory ancestry and the checkout ancestry, fails closed if a found
policy file cannot be read, and redacts matching values from displayed log
paths. On Windows or a POSIX platform without a verifiable ACL mechanism, it
fails closed before invoking Pi; do not work around that guard by redirecting
output to a shared path.

For Slurm, use `python3 scripts/pi_smoke_slurm.py`. The wrapper invokes
`sbatch` with a minimized environment, writes scheduler artifacts only inside
the same fresh ACL-verified private run directory, and redacts scheduler
diagnostics. The default
`scripts/slurm/pi_smoke.sbatch` template uses the same fixed export list and
suppresses scheduler stdout/stderr; inspect the private Pi smoke artifact for
the result. An operator-supplied `--template` remains outside this smoke
conformance boundary.

Create `.heph-private-denylist` at the repository root on machines that know
private values. Add one fixed string per line, including any private alias,
hostname, endpoint, checkpoint, or model identifier. The file is gitignored.

Before committing, run:

```bash
python3 scripts/check_private_denylist.py --staged --tracked
```

The guard prints only file paths and line numbers. It intentionally never
prints matched values or source lines.

## Automation admission

This configuration supports explicit local adapter-smoke validation. Normal
Hephaestus automation runs package/capability preflight for `--agent pi`, then
rejects stage and wrapper execution unless the host has explicitly registered
a reviewed OS-isolation adapter. The base package does not ship that adapter;
its model-visible tool flags cannot enforce the resolved filesystem or network
policy. Do not treat a local model configuration, package readiness, or a
successful smoke command as automation admission evidence.

An external adapter package exposes its zero-argument factory through the
`hephaestus.pi_isolation_adapters` Python entry-point group. A fresh console
process loads it only when the operator explicitly selects its public name:

```toml
[project.entry-points."hephaestus.pi_isolation_adapters"]
operator-broker = "operator_package.pi_broker:create_adapter"
```

```bash
export HEPH_PI_ISOLATION_ADAPTER=operator-broker
hephaestus-plan-issues --agent pi --parallel 1 --json
```

The factory must return an object implementing
`hephaestus.agents.runtime.PiIsolationAdapter`. Installing a package does not
activate it, and Hephaestus never auto-selects among installed adapters. A
missing, duplicate, unloadable, or invalid selected entry point fails before a
Pi provider process starts. The external package remains responsible for
reviewed enforcement of every filesystem and network grant it receives. It
must launch the supplied command with the supplied minimized environment. That
Pi-only environment carries a disposable `PI_CODING_AGENT_DIR` containing only
the preflight-proven packages and the operator's private model/auth files while
excluding ambient GitHub, cloud, and operator-specific variables. The broker
may inject an API credential from its own reviewed secret store; it must not
inherit arbitrary host variables. It must also return trusted observed
skill-call identifiers separately from provider text and requested grants.
See [ADR-0029](adr/0029-explicit-pi-isolation-adapter-bootstrap.md) for the
bootstrap decision and rejected automatic-loading alternatives.

## Athena package acceptance

The accepted Pi CLI and package set are recorded in the packaged catalog. Inspect
the exact subprocess plan without changing settings or executing package code:

```bash
hephaestus-install-pi-plugins --dry-run --json
```

Install globally with the safe trust default, or explicitly choose a local scope:

```bash
hephaestus-install-pi-plugins --global --yes --no-approve
hephaestus-install-pi-plugins --project-local --yes --approve
pi --version
```

Global scope and `--no-approve` are defaults. Project-local `--approve` applies
only to the verification process and is not persisted. A CLI identity/version
mismatch fails before package installation or extension loading. Partial
installs are retained and reported; rerun the same command to recover.

Pins change only through a reviewed catalog update. Hephaestus maintainers rerun
the Athena acceptance workflow for an Athena commit change and the live package
smoke for any Pi CLI or npm companion change. Roll back by reverting the catalog
and reinstalling its prior exact pins; `pi update` is not an ownership path.

Athena exposes its canonical `skills/` directory as Pi package resources.
Mnemosyne remains a separately trusted repository dependency governed by
Athena's canonical dependency-resolution contract; it is not a Pi package.
This Pi package contract does not gate host-owned Athena `advise` or `learn`.
Those operations use Hephaestus's provider-neutral Athena contract and do not
invoke or preflight any agent harness. Pi validation applies only to work that
actually executes through `--agent pi`.
The reviewed external capabilities remain separate catalog-pinned packages:
`pi-subagents` supplies delegation and `pi-web-access` supplies explicitly
scoped web access. Athena bundles neither package.

Issue #2519 captures the live Pi/Codex conformance evidence for this provider
boundary. The private run artifacts live under `build/pi-e2e-2519/<run-id>/`
and the reproducible publication outputs are `docs/pi-e2e-2519-report.md`
and `docs/runbooks/pi-e2e-2519.md`.

### Acceptance collection and publication

After the upstream package and this implementation PR exist, use clean Athena
and Hephaestus checkouts and the pinned Pi binary to collect evidence:

```bash
uv run python scripts/pi_package_acceptance.py collect \
  --athena-checkout "$ATHENA_CHECKOUT" \
  --implementation-pr "$HEPHAESTUS_PR_NUMBER" \
  --pi-bin "$ATHENA_PI_BIN" \
  --output-dir build/pi-acceptance
```

Collection performs GitHub reads only. It validates the catalog, exact Athena
PR/tag/check correspondence, clean checkout revisions, deterministic archive
contents, exact-ref installation, and package-origin RPC discovery of
`skill:advise`, `skill:learn`, and `skill:pr-review`. It generates
`build/pi-acceptance/acceptance.json` and
`build/pi-acceptance/issue-comment.md` solely from observed results. These
artifacts are untracked evidence and must not be handwritten or committed.

Inspect both artifacts, then explicitly publish and read back the actor-owned
issue comment:

```bash
uv run python scripts/publish_pi_package_acceptance.py \
  --acceptance build/pi-acceptance/acceptance.json \
  --comment build/pi-acceptance/issue-comment.md
```

The publisher is the only forge-write step. It revalidates the catalog and
implementation PR, refuses foreign or duplicate marker comments, and succeeds
only after exact actor-owned readback. If acceptance must be rolled back,
publish evidence for the reviewed prior catalog ref; never retag `v0.4.0` or
silently advance the existing catalog.

## Project-level denylist (committed)

`.heph-project-denylist` is committed to the repo and scanned in CI for every
contributor, so the privacy policy is effective even without a local file. Add
only patterns safe to name in a public repo (deprecated hostnames, banned
placeholder leaks). Genuine operator secrets still go in the untracked
`.heph-private-denylist`. Both files use one fixed string per line; blank lines
and `#`-comments are ignored. The two lists are merged and de-duplicated.
