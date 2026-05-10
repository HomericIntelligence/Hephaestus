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
- [ ] `pyproject.toml` `[project].version` matches the **current** released version (the tag step
  will increment it in the tag name, but the package version at HEAD must equal the *new* tag or
  the "verify tag matches package version" step will fail). Update `pyproject.toml` and run
  `pixi run python -m hephaestus.version.manager` before merging if needed.
- [ ] `pixi.lock` is up to date (`pixi install` produces no changes).

## Manual Tag + Release (escape hatch)

If `auto-tag.yml` is skipped and a tag was pushed manually, the **Release** workflow also accepts
a `workflow_dispatch` with an optional explicit `tag` input. Leave it blank to use the latest tag,
or supply a tag name (e.g. `v0.8.0`) to release a specific ref.

## PyPI Trusted Publishing

Publishing uses OIDC trusted publishing — no `PYPI_API_TOKEN` secret is needed. The workflow runs
in the `pypi` GitHub Environment which must have the corresponding trusted publisher configured on
PyPI. See the [PyPI documentation](https://docs.pypi.org/trusted-publishers/) for setup.
