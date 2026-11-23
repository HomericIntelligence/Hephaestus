# ADR-0072: Codex admission keeps one reviewed artifact manifest and one unsigned-tree digest

- Status: Accepted
- Date: 2026-11-23
- Tracks: #3697, #3504; follows #3609 and PR #3610

## Context

PR #3610 replaced the in-repo byte pins with signature and version contracts. Two
byte-identity pins survived on purpose.

The first pin is the real-release Codex canary in
`tests/integration/test_codex_adapter_admission_artifact.py`. It is the only test
that runs the full production admission chain against a real Codex release and
real Sigstore evidence. ADR-0042 already requires a tracked manifest for this
fixture. The test and the CI lane still repeated the release tag, asset names,
digests, and Rekor identity as Python and shell constants. A release bump
therefore needed edits in several files, and those copies could disagree.

The second pin is `CODEX_ATHENA_ARTIFACT_SHA256` in
`hephaestus/agents/runtime.py`. The Athena Codex cache is a plain checkout. No
signature covers the exact bytes that enter the cache. A signed tag
authenticates the tag object only. A checksum from the same release is not a
signature.

## Decision

1. Keep the real-release canary. Do not remove the only live Sigstore path.
   `tests/fixtures/sigstore/codex-sigstore-fixture.json` is the single source for
   the release tag, the asset names and IDs, the ELF digest, the retained
   evidence names, and the Rekor log identity (schema version 2). The
   provisioner, the integration test, and the CI lanes read these values from
   the manifest. The provisioner prints the validated tag with `--print-tag`.
   The fixture root takes the tag as its final path component. A release bump is
   one reviewed manifest change plus refreshed retained evidence. It is not one
   scalar value: the bundle, Rekor evidence, and sometimes the trusted root also
   change.

2. Keep `CODEX_ATHENA_ARTIFACT_SHA256` and its check before the Athena package
   enters a Codex process. Remove it only after all three conditions hold:
   Athena publishes a signature that covers the exact cache bytes that enter
   the Codex cache; Hephaestus has an offline verifier with a reviewed trust
   root or public key; and a real-artifact test verifies a signed Athena cache
   and fails closed on tamper.

3. Do not treat a signed tag or a same-release checksum as a signature
   substitute. Do not move the Athena digest into the Codex deployment lock. The
   lock governs the adapter deployment. The Athena package is a private-profile
   input, and a lock schema change would couple unrelated identities.

## Revisit triggers

- The supported Codex compatibility line moves, and the canary must move with
  it.
- The pinned upstream release or asset is removed.
- The Sigstore trusted root retires the pinned Rekor log, or verifier policy
  rejects the pinned evidence.
- The OpenAI release workflow identity changes.
- Athena publishes a signed artifact that covers the exact cache bytes, and
  the offline verification and real-artifact test conditions hold.
- The Athena cache layout changes.

## Alternatives considered

- **Static re-pin of the test constants.** Rejected. It creates a second
  authority that can disagree with the manifest.
- **Remove the real-release canary.** Rejected. No other test runs the full
  production admission chain against real shipped artifacts.
- **Remove the Athena digest when a signed tag exists.** Rejected. The signed
  tag does not authenticate the cache tree.
- **Move the Athena digest into the deployment lock.** Rejected. It couples the
  plugin identity to the adapter identity and adds no current requirement.
- **Use a same-release checksum as a signature.** Rejected. A checksum from the
  same origin does not establish a reviewed signer.

## Consequences

- A bump needs one reviewed manifest update and refreshed retained evidence.
  Python, test, and CI paths do not change.
- The canary runs in the nightly `build` job, not in required PR checks. A
  real-artifact regression can merge before the nightly run finds it. This is
  the accepted residual risk.
- The manifest cannot make old evidence valid under a new trusted root. If the
  root retires the pinned log, refresh the evidence and review the root update.
- The Athena digest blocks an Athena update until a reviewer updates the digest
  and the negative test still fails closed. This is intended.
- The Sigstore chain verification, the detached deployment lock as the runtime
  authority, the installed-tree inventory, and the fail-closed staging path do
  not change. The Pi and npm version pins and the agent-contract pin do not
  change.
