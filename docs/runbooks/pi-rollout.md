# Pi rollout and recovery

This runbook covers the managed Pi provider path. It does not change host-owned
Athena `advise` or `learn`, and it does not grant Pi automation by itself.

## Enable Pi

1. Install the pinned Pi CLI and managed package set.

   ```bash
   npm install -g --ignore-scripts @earendil-works/pi-coding-agent@0.80.2
   hephaestus-install-pi-plugins --global --yes --no-approve
   ```

2. Inspect the exact package plan when you need a dry run.

   ```bash
   hephaestus-install-pi-plugins --dry-run --json
   ```

3. Run Pi only after the host adapter and Pi package contract are ready.

   ```bash
   hephaestus-automation-loop --agent pi
   ```

## Omit Pi

- Pass `--disable-pi-automation` to the loop or stage command when Pi must stay
  out of a run.
- Host-owned Athena `advise` and `learn` remain available when Pi is omitted.

## Recover managed Pi state

- Re-run `hephaestus-install-pi-plugins --global --yes --no-approve` after a
  catalog update or a local repair.
- If `--agent pi` fails, confirm the pinned CLI is present, the managed package
  set is installed, and the selected adapter entry point is valid.

