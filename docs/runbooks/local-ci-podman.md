# Runbook: Recover the Local CI Podman Machine

Use this runbook when the macOS local CI runner reports
`HEPHAESTUS_CI_RUNNER_FAILURE: container-engine-unavailable` for Podman.

## Contain the failure

Keep the native fallback active until all container health checks pass. Do not
change `scripts/run_ci_local.sh` to manage Podman machines. Machine deletion is
an operator action and is outside the CI runner authority.

Use a persistent host terminal for all machine commands. Keep the terminal open
until the machine and container checks are complete. This keeps the AppleHV
helper process lifetime separate from a short-lived automation command.

## Inspect the host state

List all machines and system connections before you change state:

```bash
podman machine list
podman system connection list
podman machine inspect hephaestus-ci
```

If a start command does not complete, inspect the global start lock and the
related processes. Do not terminate a process until the operator gives explicit
approval for that exact process.

```bash
lsof /Users/<user>/.local/share/containers/podman/machine/machine-start.lock
ps -o pid,ppid,etime,command -p <pid>
```

Collect the applicable machine serial log before you change the machine:

```bash
tail -n 200 /private/var/folders/<host-path>/T/podman/hephaestus-ci.log
```

## Get approval

Get explicit approval to delete only the hephaestus-ci machine. Deletion removes the
images, containers, and other state that the machine stores. There is no
rollback for this machine-local state. The native fallback is the recovery path
if recreation does not succeed.

Do not continue if the approval does not name the hephaestus-ci machine.

## Recreate the machine

Remove only the approved machine:

```bash
podman machine rm hephaestus-ci
```

Do not remove `podman-machine-default`. Do not use a broad Podman reset command.

Create the replacement with 8 GiB of memory. The CI image build can use more
than 4 GiB when APT installs the validation tools. Start the machine and make
its connection active:

```bash
podman machine init --cpus 4 --disk-size 30 --memory 8192 --provider applehv --now --update-connection hephaestus-ci
```

## Verify the machine

Verify that the machine is running and that hephaestus-ci is the active
connection:

```bash
podman machine inspect hephaestus-ci
podman system connection list
podman info
```

Stop if `podman info` cannot connect to the server. Keep the native fallback
active. Capture the serial log and the owner of `machine-start.lock`, if one
exists. Do not delete or stop a different machine.

## Verify the local CI path

Build the local image from the current reviewed checkout:

```bash
podman build -f ci/Containerfile -t hephaestus-ci:local .
```

Run the complete local CI runner through Podman:

```bash
CONTAINER_ENGINE=podman bash scripts/run_ci_local.sh all --rebuild
```

The command must exit with status 0. Its output must not contain
`HEPHAESTUS_CI_RUNNER_FAILURE`. Keep the native fallback active until these
health checks pass.

## Record the result

Record the machine name, Podman client and server versions, and the exact
verification commands in the related GitHub issue. Do not record credentials,
private environment values, or the contents of machine identity files.
