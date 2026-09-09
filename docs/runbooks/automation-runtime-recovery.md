# Recover with a fixed automation runtime

Use this procedure when an installed coordinator lacks a merged repair.
Updating a target repository does not update the executing Python package.
Keep the runtime checkout separate from the repositories that the loop changes.

## Prepare the runtime

Select a reviewed commit on `main` with passing required checks. In a separate
checkout of that commit, run these commands. Do not use a working branch with
uncommitted repairs as the production runtime.

```sh
HEPHAESTUS_REVISION=$(git rev-parse HEAD)
uv sync --locked --no-editable --python 3.13
uv pip install --python .venv/bin/python --no-deps --reinstall \
  "HomericIntelligence-Hephaestus @ git+https://github.com/HomericIntelligence/Hephaestus.git@${HEPHAESTUS_REVISION}"
```

The locked environment includes the automation and development dependencies.
The VCS installation records the full installed commit in `direct_url.json`.
Use the absolute console-script path from this environment for recovery.
Do not update this environment while its coordinator is running.

## Check the runtime identity

The loop logs a `runtime_identity` record before repository or agent work.
It contains the launcher, interpreter, environment prefix, package location,
distribution version, and installed commit. A missing VCS commit is reported
as `None`; the target repository HEAD is never substituted for it.

For a source-only or wheel installation, record the artifact provenance
separately. A development version string alone is not a full commit identity.

On macOS, a live implementation run rejects an environment without
`pyvenv.cfg`. Use the launcher inside `.venv`, not a Conda base launcher.
Host runtime copying also rejects that environment. A preparation failure
reports its step and exception type without the underlying private text.

## Resume one issue

Inspect the retained writer, local changes, and live PR before a new run.
Preserve uncommitted changes and any unresolved rebase. From the target
repository checkout, invoke the absolute runtime launcher:

```sh
/absolute/runtime/.venv/bin/hephaestus-automation-loop \
  --issues 2623 --loops 1 --max-workers 1 --verbose
```

Replace `2623` with the selected issue number. Capture both output streams and
the process exit status under `build/`. Keep the provider defaults unless the
operator selects another provider. Confirm the logged installed commit before
the run proceeds. Confirm completion from the live PR merge and issue state.

Do not repeat a failed run until its cause or input has changed. A GitHub
connectivity failure does not invalidate a published plan. Read that plan
again after connectivity recovers before requesting another planning job.
