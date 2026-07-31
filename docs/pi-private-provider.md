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
and temporary-directory environment. It does not forward the `HEPH_PI_*`
sentinels, arbitrary Pi settings, GitHub/cloud credentials, or an operator's
telemetry preference; it forces `PI_TELEMETRY=0` and
`PI_SKIP_VERSION_CHECK=1`. A generated Pi session ID is discarded and never
printed or written to the diagnostic artifact.

The smoke command writes a local diagnostic artifact and requires user-only
file permissions for it. It loads `.heph-private-denylist` from both its
working-directory ancestry and the checkout ancestry, fails closed if a found
denylist cannot be read, and redacts matching values from displayed log paths.
On Windows it fails closed before invoking Pi until a user-only ACL
implementation is available; do not work around that guard by redirecting
output to a shared path.

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
Hephaestus automation intentionally rejects `--agent pi` until #2516 verifies
the required package/capability inventory and #2518 enforces lifecycle and
tool scopes. Do not treat a local model configuration or a successful smoke
command as automation admission evidence.

## Project-level denylist (committed)

`.heph-project-denylist` is committed to the repo and scanned in CI for every
contributor, so the privacy policy is effective even without a local file. Add
only patterns safe to name in a public repo (deprecated hostnames, banned
placeholder leaks). Genuine operator secrets still go in the untracked
`.heph-private-denylist`. Both files use one fixed string per line; blank lines
and `#`-comments are ignored. The two lists are merged and de-duplicated.
