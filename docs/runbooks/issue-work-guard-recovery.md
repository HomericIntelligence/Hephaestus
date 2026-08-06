# Recovering an issue work guard

`state:in-progress` is a visible automation ownership guard. It is
orthogonal to the plan labels and is not proof that a plan transition is
authorized. Do not remove it manually or use the normal automation token to
clear it.

## Inspect

Use the read-only inspection mode to capture the current ref OID, claim UUID,
lease expiry, phase, actor, and preserved plan labels:

```bash
hephaestus-recover-issue-guard --repo OWNER/REPO --issue ISSUE --inspect
```

The output is diagnostic only. If the claim is active and its lease plus the
ten-minute recovery grace have not elapsed, stop and let the owner renew or
release it.

## Recover an abandoned claim

An operator must obtain a separate recovery credential and use an actor in the
repository's recovery allowlist. This credential must not be `GH_TOKEN` or
`GITHUB_TOKEN`, and it must not be present in normal automation:

```bash
export HEPHAESTUS_GUARD_RECOVERY_TOKEN='operator-secret'
hephaestus-recover-issue-guard \
  --recover \
  --repo OWNER/REPO \
  --issue ISSUE \
  --expected-claim CLAIM_UUID \
  --expected-oid REF_OID \
  --reason 'confirmed abandoned runner after incident INC-123'
```

The command fails closed if the expected claim or OID changed, the lease grace
has not elapsed, the ref/label pair is inconsistent, or the recovery actor is
not allowlisted. It removes only the guard label and preserves every normal
plan label, including `state:plan-blocked`; an external actor must still
resolve a blocked plan.

After recovery, retain the command output and the ref history with the incident
record. A new automation run may acquire the issue after it observes the
terminal recovery record and the absent guard label.
