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

The version itself does **not** need to be edited in any file: this project uses
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
