# Runbook: No Silent Failures

This repository forbids the family of workarounds that hide a real failure
behind a passing exit code. A `|| true`, a `continue-on-error: true`, or an
advisory `::warning::` that replaces a tool's non-zero exit all produce the
same outcome: CI goes green while the underlying problem ships unfixed.

Four `local` pre-commit hooks in `.pre-commit-config.yaml` enforce this policy.

## The hooks

- **`forbid-or-true`** — rejects the `|| true` idiom (and whitespace variants)
  in shell, YAML, Dockerfile, justfile, and HCL sources. `cmd || true` discards
  the real exit status of `cmd`.
- **`forbid-continue-on-error`** — rejects `continue-on-error: true` in GitHub
  Actions workflow files. A step that "continues on error" lets a failing job
  report success.
- **`forbid-advisory-warnings`** — rejects the `::warning::` annotation in
  workflow `run` blocks. When `::warning::` replaces a tool's failure exit with
  a benign-looking annotation, the step still passes and the problem still goes
  unfixed.
- **`forbid-unwhitelisted-add-to-bashrc`** — a related safety hook: it forbids
  any `add_to_bashrc "..."` call whose argument is not one of the two
  whitelisted shapes, because `add_to_bashrc` passes its argument to `eval`.

## Why silent failures are banned

A failing command should fail loudly. Suppressing the exit code:

- hides regressions until they surface somewhere far more expensive to debug;
- erodes trust in a green build — "passing" stops meaning "correct";
- accumulates as invisible tech debt that no dashboard ever surfaces.

The cost of a loud failure now is always lower than the cost of a silent one
discovered later.

## Compliant alternatives

Replace the suppression with an explicit decision. If a non-zero exit is truly
acceptable, say so in code and handle it.

Non-compliant:

```bash
flaky-check || true
```

Compliant — handle the failure explicitly:

```bash
if ! flaky-check; then
  echo "flaky-check failed; <documented reason it is non-fatal here>" >&2
  # take a real action: fall back, retry, or exit non-zero
fi
```

For a bracketed region where you genuinely want to inspect exit codes, use a
documented `set +e` / `set -e` pair:

```bash
set +e
output=$(some-command)
status=$?
set -e
# now branch on $status explicitly
```

For GitHub Actions, fix the underlying job instead of `continue-on-error: true`.
The only legitimate use of `::warning::` is annotating a step that runs *before*
the failing tool (for example, advising on a deprecated config), never wrapping
the tool's own exit.

## How to fix a tripped hook

1. Read the hook output — it names the offending file and line.
2. Locate the suppression (`|| true`, `continue-on-error: true`, `::warning::`,
   or a non-whitelisted `add_to_bashrc`).
3. Replace it with an explicit `if`-guard, a real conditional, or a documented
   `set +e` / `set -e` bracket, as shown above.
4. Re-run `pre-commit run --all-files` (never `--no-verify`) to confirm.

If you believe a match is a genuine false positive, fix the hook's regex rather
than suppressing the policy — and extend any paired allow-list in the same
commit.
