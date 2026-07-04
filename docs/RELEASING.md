# Releasing ProjectHephaestus

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
  └─ git tag + push → triggers:
       Release workflow (on: push: tags: v*)
         ├─ test job (pytest)
         ├─ type-check job (mypy)
         └─ build-and-publish job
              ├─ verify tag == package version
              ├─ build wheel + sdist
              ├─ publish to PyPI (trusted publishing)
              └─ create GitHub Release with auto-generated notes
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

If `auto-tag.yml` is skipped and a tag was pushed manually, the **Release** workflow also accepts
a `workflow_dispatch` with an optional explicit `tag` input. Leave it blank to use the latest tag,
or supply a tag name (e.g. `v0.8.0`) to release a specific ref.

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

## Rollback

A release produces a published-then-immutable PyPI artifact plus a git tag and a
GitHub Release. Options are listed below by escalating impact; prefer the lowest-impact
option that addresses the problem.

### 1. Yank from PyPI (recommended for shipped-but-broken releases)

[Yanking](https://pypi.org/help/#yanked) keeps the release on PyPI for users who pin
the exact version, but hides it from new resolvers. Existing installs are not affected.

Yanking is performed by a project owner from the PyPI web UI:

```text
PyPI project page → Manage → Releases → vX.Y.Z → Options → Yank
```

### 2. Edit or delete the GitHub Release

```bash
# Mark as pre-release (hides from "Latest"):
gh release edit vX.Y.Z --prerelease

# Or delete the GitHub Release entry:
gh release delete vX.Y.Z --cleanup-tag    # also deletes the git tag
gh release delete vX.Y.Z                  # GitHub Release only; tag stays
```

The release workflow is **idempotent** (see #432): re-running it on an existing tag
re-attaches build artifacts without duplicating the GitHub Release.

### 3. Ship a hotfix (preferred for forward-fix)

Yanking does not retract a bad release for users already on it. The cleanest fix is
a new patch release:

```bash
# 1. Branch from the bad tag
git checkout -b hotfix/vX.Y.Zp1 vX.Y.Z

# 2. Apply the fix (with tests). One issue per PR per CONTRIBUTING.md.

# 3. Open + merge a PR targeting main.

# 4. Trigger the Auto Tag Release workflow with bump_kind=patch.
```

The new patch publishes via the normal release pipeline and supersedes the bad
release for new installs.

### 4. Delete the git tag (last resort)

Once a tag is pushed and a PyPI release published, deleting the tag does not
unpublish PyPI and breaks reproducibility for anyone who fetched the tag. Use only
for tags that **never** reached PyPI:

```bash
git push --delete origin vX.Y.Z
git tag -d vX.Y.Z
```

### Recovery summary

| Situation | Action |
|-----------|--------|
| Bad release on PyPI, fix is ready | Ship a patch (option 3); optionally yank the bad version (option 1). |
| Bad release on PyPI, no fix yet | Yank (option 1) + mark the GitHub Release as pre-release (option 2). |
| Tag pushed but PyPI publish failed | Delete the tag (option 4) and re-trigger after fixing the workflow. |
| GitHub Release notes are wrong | `gh release edit vX.Y.Z --notes-file …` — no PyPI impact. |
