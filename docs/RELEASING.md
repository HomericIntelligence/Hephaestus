# Releasing Hephaestus

## One-Click Release (normal path)

1. Go to **Actions → Auto Tag Release → Run workflow**.
2. Choose `bump_kind`:
   - `patch` (default) — bug fixes, e.g. `0.7.3 → 0.7.4`
   - `minor` — backwards-compatible features, e.g. `0.7.3 → 0.8.0`
   - `major` — breaking changes, e.g. `0.7.3 → 1.0.0`
3. Click **Run workflow**.

That is the only manual step. The pipeline then runs automatically:

```
workflow_dispatch (Auto Tag Release)
  └─ computes next vX.Y.Z
  └─ git tag + push (signed annotated tag)
  └─ workflow_dispatch (Release workflow, tag=vX.Y.Z)
         ├─ test job (pytest)
         ├─ type-check job (mypy)
         ├─ release-preflight (remote tag + unused publication targets)
         ├─ publish-testpypi job
         │      ├─ build wheel + sdist
         │      ├─ publish to TestPyPI (trusted publishing)
         │      └─ smoke-install from TestPyPI (retries for index propagation)
         └─ build-and-publish job (after publish-testpypi succeeds)
              ├─ verify tag == package version
              ├─ reuse dist built by publish-testpypi
              ├─ stage a GitHub draft with every asset
              ├─ publish to PyPI (trusted publishing)
              └─ publish the prepared GitHub draft
```

## Pre-Release Checklist

Before triggering the workflow, ensure:

- [ ] All changes merged to `main` and CI is green.
- [ ] `pixi.lock` is up to date (`pixi install` produces no changes).
- [ ] No open issues in the milestone you are releasing.
- [ ] `docs/MIGRATION.md`'s "latest released version is **X.Y.Z**" line already names
  the version you are about to tag. The Release workflow's test job runs
  `test_migration_md_version_does_not_trail_latest_git_tag`, and tags are immutable —
  tagging before the doc bump strands the tag unreleased (this broke the first
  v0.9.7 attempt and all of v0.9.8; see #1802). Bump the doc, merge, then tag.

The package version itself does **not** need to be edited in any file: this project uses
hatch-vcs dynamic versioning, so the package version is derived from the git tag the
`auto-tag` workflow pushes. There is no `[project].version` field to bump.

## Manual Tag + Release (escape hatch)

If `auto-tag.yml` is skipped and a tag was pushed manually, dispatch the **Release** workflow with
the exact existing `vX.Y.Z` tag. The manual input is required: the workflow never infers a moving
"latest" tag.

### Dispatch failed after tag push

`auto-tag.yml` performs two writes in sequence: it pushes the signed tag, then dispatches the
Release workflow with that tag. If the dispatch step fails (transient API error, token or
permission problem), the tag is already on the remote but no release run exists — the tag is
stranded.

**Never re-run Auto Tag Release to recover.** It computes the next version from the highest
existing tag, so a re-run sees the stranded tag and computes and pushes a **new** tag (e.g.
stranded `v0.9.10` → re-run creates `v0.9.11`), leaving the original tag unreleased forever
(tags are immutable; see #1802 for how stranded tags happen).

Instead, dispatch the Release workflow directly with the stranded tag named explicitly:

```bash
gh workflow run release.yml -f tag=vX.Y.Z   # the exact tag auto-tag pushed
```

Never dispatch with a blank `tag` input in this state. Blank means "latest tag", which is only
correct until another tag lands between the failure and your recovery — always name the
stranded tag.

## TestPyPI Trusted Publishing

Before this staging gate can pass, a `testpypi` GitHub Environment must exist on this
repository with its own trusted publisher registered on
[test.pypi.org](https://test.pypi.org) (a separate index from pypi.org, requiring its
own trusted-publisher registration — the `pypi` trusted publisher below does not cover
it). One-time setup:

1. On test.pypi.org, register a pending trusted publisher for this project: owner
   `HomericIntelligence`, repository `Hephaestus`, workflow `release.yml`, environment
   name `testpypi`.
2. In the GitHub repo, create an environment named `testpypi` (Settings → Environments).
   A required reviewer is optional here (unlike `pypi`) since a bad TestPyPI publish is
   not user-facing.
3. No secrets are needed — like `pypi`, this uses OIDC trusted publishing.

Until this one-time setup is complete, `publish-testpypi` fails at the OIDC-publish step
with an authorization error, and `build-and-publish` (which needs `publish-testpypi`)
never runs.

## PyPI Trusted Publishing

Publishing uses OIDC trusted publishing — no `PYPI_API_TOKEN` secret is needed. The workflow runs
in the `pypi` GitHub Environment which must have the corresponding trusted publisher configured on
PyPI. See the [PyPI documentation](https://docs.pypi.org/trusted-publishers/) for setup.

## GPG signing keys for auto-tag

Release tags pushed by `.github/workflows/auto-tag.yml` are **GPG-signed annotated tags**
(`git tag -s`). This matches the repo-wide signed-commits policy: every commit on `main` is
signed, so every tag created from those commits should carry the same cryptographic provenance.

### Required repository secrets

`auto-tag.yml` imports a GPG key via [`crazy-max/ghaction-import-gpg`](https://github.com/crazy-max/ghaction-import-gpg)
(pinned to a commit SHA — Dependabot's `github-actions` ecosystem already watches it). The
action reads two repository secrets:

| Secret | Purpose |
|--------|---------|
| `GPG_PRIVATE_KEY` | The ASCII-armored export of the signing key (starts with the standard PGP private-key block header). |
| `GPG_PASSPHRASE` | Passphrase that unlocks `GPG_PRIVATE_KEY`. Set even if the key has none — leave empty. |

Without both secrets the workflow **fails on the import step before any tag is created**.
That is intentional: there is no half-state where an unsigned tag is pushed because secrets
are missing.

### Key requirements

- **Algorithm**: RSA 4096 (or Ed25519). RSA 2048 is acceptable but discouraged for new keys.
- **User ID email**: must match the `user.email` the workflow configures
  (`github-actions[bot]@users.noreply.github.com`) so verification clients accept the signature.
- **Expiry**: at least 1 year past the next planned release cadence. A key that expires
  mid-cycle silently breaks `auto-tag.yml` on its next run.
- **Subkey scope**: a signing-capable subkey is sufficient; the primary key does not need to
  be uploaded.

### Initial setup

```bash
# 1. Generate a dedicated release-signing key (on a trusted machine).
gpg --quick-gen-key 'github-actions[bot] <github-actions[bot]@users.noreply.github.com>' \
    rsa4096 sign 2y

# 2. Export the armored private key + record the passphrase used above.
KEY_ID=$(gpg --list-secret-keys --keyid-format=long --with-colons \
         'github-actions[bot]@users.noreply.github.com' \
         | awk -F: '/^sec/ {print $5; exit}')
gpg --armor --export-secret-keys "${KEY_ID}" > /tmp/auto-tag-private.asc

# 3. Upload to GitHub repo secrets.
gh secret set GPG_PRIVATE_KEY < /tmp/auto-tag-private.asc
gh secret set GPG_PASSPHRASE   # paste passphrase when prompted

# 4. Wipe the local export.
shred -u /tmp/auto-tag-private.asc

# 5. Publish the corresponding public key so consumers can verify tags.
gpg --armor --export "${KEY_ID}" | gh release upload <some-release> -    # or push to keys.openpgp.org
```

### Rotation

Plan rotation **before** the existing key expires. The procedure is the same as initial setup
plus a verification dry-run:

1. Generate the new key (steps 1-2 above).
2. `gh secret set GPG_PRIVATE_KEY` + `gh secret set GPG_PASSPHRASE` — this overwrites both
   atomically.
3. Trigger `Auto Tag Release` via **Actions → Run workflow** with `bump_kind=patch` on a
   throwaway test branch (or against a non-`main` ref) and confirm the import step succeeds.
4. Revoke the old key once a release cycle has passed.

### Failure modes

| Symptom | Diagnosis |
|---------|-----------|
| `Error: gpg: ... no secret key` on the import step | `GPG_PRIVATE_KEY` secret is missing or truncated; re-upload the armored export. |
| Import step succeeds but `git tag -s` fails with `gpg: signing failed` | Passphrase mismatch — `GPG_PASSPHRASE` does not unlock `GPG_PRIVATE_KEY`. |
| `gpg: signing failed: Inappropriate ioctl for device` | Missing `GPG_TTY` — the import action sets this; if the failure recurs, re-pin to the latest version. |
| Workflow ran fine yesterday, fails today with `gpg: key ... has expired` | The signing key expired. Rotate per the procedure above and re-trigger. |

If the import step fails for any reason, no tag is created and no release artifact is produced
(see the idempotency guarantee in #432).

## Rollback and withdrawal

Package files and published immutable-release assets cannot be rolled back, replaced, or attached
later. A release withdrawal preserves the tag and asset fingerprints: yank the PyPI version, add a
withdrawal advisory to the matching immutable GitHub Release, then ship a corrected patch version.

Do not upload packages outside the globally serialized release workflow while it is active. If API
state is not clearly one of the cases below, stop and escalate rather than deleting tags, releases,
or package files speculatively.

```bash
# 1. Read-only inspection/rehearsal against live APIs.
pixi run python scripts/release_rollback.py inspect --tag vX.Y.Z

# 2. Yank X.Y.Z in the PyPI project UI and include the incident/fix reason.

# 3. Verify the yank and add a withdrawal warning to GitHub release notes.
GH_TOKEN="$(gh auth token)" \
pixi run python scripts/release_rollback.py rollback \
  --tag vX.Y.Z \
  --reason "Broken because <reason>; use X.Y.(Z+1)." \
  --apply \
  --confirm-tag vX.Y.Z

# 4. Implement, review, and publish the corrected patch through the normal workflow.
```

Yanking is deliberately performed through the PyPI project UI. It is non-destructive: exact pins
can still select the withdrawn version, while new resolution avoids it. The checked-in command is
read-only until `--apply`, then verifies that every PyPI file is yanked and PATCHes only GitHub
release notes. It refuses malformed, unavailable, or contradictory API state.

### Recovery matrix

| Observed state | Recovery |
| --- | --- |
| TestPyPI only; no PyPI or published GitHub release | Remove the TestPyPI version and any GitHub draft, fix the cause, then explicitly redispatch the same tag. |
| PyPI published; GitHub draft complete; package is good | Verify draft assets, then publish that draft; do not upload replacement bytes. |
| PyPI published; package is broken | Yank with a reason, publish or retain the matching GitHub record, apply the withdrawal advisory, then ship a new patch. |
| Immutable GitHub release published | Never replace or delete its assets or reuse its tag; edit notes only and forward-fix. |
| Any API state is incomplete or contradictory | Stop and escalate; do not delete tags, releases, or package files speculatively. |
