# Private Pi Provider Setup

Configure private OpenAI-compatible providers only in the operator-local Pi
configuration, for example under `~/.pi/agent/models.json`. Do not commit Pi
provider config, endpoint URLs, hostnames, checkpoint names, model identifiers,
or operator-local aliases.

Use placeholders in documentation:

- `<operator-local-alias>`
- `<private-provider-url>`
- `<private-model-name>`

Install the real Pi CLI in the automation environment; do not substitute a fake
`pi` binary for adapter validation:

```bash
npm install -g --ignore-scripts @earendil-works/pi-coding-agent@0.80.2
pi --version
```

Set the local alias at runtime:

```bash
export HEPH_PI_MODEL=<operator-local-alias>
python3 scripts/pi_smoke.py
```

Create `.heph-private-denylist` at the repository root on machines that know
private values. Add one fixed string per line, including any private alias,
hostname, endpoint, checkpoint, or model identifier. The file is gitignored.

Before committing, run:

```bash
python3 scripts/check_private_denylist.py --staged --tracked
```

The guard prints only file paths and line numbers. It intentionally never prints
matched values or source lines.
