# Runbook: Recover the Local CI Podman Machine

Use this runbook when the macOS local CI runner reports
`HEPHAESTUS_CI_RUNNER_FAILURE: container-engine-unavailable` for Podman.

## Contain the failure

Keep the native fallback active until all container health checks pass. Do not
change `scripts/run_ci_local.sh` to manage Podman machines. Machine deletion is
an operator action and is outside the CI runner authority.

Start the automation loop with its host-owned Podman supervisor:

```bash
hephaestus-automation-loop --podman-machine hephaestus-ci <other-options>
```

The foreground automation-loop process owns the AppleHV helper lifetime. Before
pipeline dispatch, the supervisor inspects only the selected machine. It starts
the machine if it is stopped and runs a bounded health check against its named
connection. It requires the AppleHV provider, a running state, and a nonempty
`LastUp` value. It also compares the selected named connection with the machine's
inspected SSH port, remote user, and identity before it runs the health check.
If a check fails, it records the `machine-start.lock` owner from the approved
Podman data directory and at most 200 serial-log lines. The serial-log read and
encoded detail are each limited to 16 KiB. It then stops pipeline dispatch. It does not stop, remove, or
recreate a machine.

The selected connection reaches the verified local CI runner through the loop
configuration. It does not change the global Podman default. Ambient
`CONTAINER_CONNECTION` and `CONTAINER_ENGINE` values do not select this loop
connection. Immutable host verification keeps its separate execution boundary.

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

If the existing machine passes its named-connection health check, skip recreation.

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
than 4 GiB when APT installs the validation tools. Start the machine without changing the default connection:

```bash
podman machine init --cpus 4 --disk-size 30 --memory 8192 --provider applehv --now --update-connection=false hephaestus-ci
```

## Verify the machine

Verify that the machine is running and that its named connection is healthy:

```bash
podman machine inspect hephaestus-ci
podman system connection list
podman --connection hephaestus-ci info
```

In the `podman machine inspect` output, verify that `ConfigDir.Path` ends in
`/applehv`. Also verify that `LastUp` is not empty or the zero timestamp. These
checks prove that the replacement uses AppleHV and completed at least one boot.

Stop if `podman --connection hephaestus-ci info` cannot connect to the server. Keep the native fallback
active. Capture the serial log and the owner of `machine-start.lock`, if one
exists. Do not delete or stop a different machine.

## Verify the local CI path

Build the local image from the current reviewed checkout:

```bash
podman --connection hephaestus-ci build -f ci/Containerfile -t hephaestus-ci:local .
```

Run the complete local CI runner through Podman:

```bash
CONTAINER_CONNECTION=hephaestus-ci CONTAINER_ENGINE=podman bash scripts/run_ci_local.sh all --rebuild
```

The command must exit with status 0. Its output must not contain
`HEPHAESTUS_CI_RUNNER_FAILURE`. Keep the native fallback active until these
health checks pass.

## Record the result

Record the machine name, Podman client and server versions, and the exact
verification commands in the related GitHub issue. Do not record credentials,
private environment values, or the contents of machine identity files.
