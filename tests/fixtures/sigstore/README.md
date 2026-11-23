# Sigstore test fixtures

These Base64 files retain three unchanged upstream Sigstore test objects:

- `test/assets/bundle_v3.txt`
- `test/assets/bundle_v3.txt.sigstore`
- `test/assets/staging-tuf/targets/ed6a9cf4e7c2e3297a4b5974fce0d17132f03c63512029d7aa3a402b43acab49.trusted_root.json`

The source repository is `sigstore/sigstore-python`. The source revision is
`27db13db157ecae5606eb22ec0be299ef348c370`. The upstream repository supplies
these files under the Apache License 2.0. The behavior test decodes the files
in memory. It denies network access before it calls the real offline verifier.

The Codex files retain the small release evidence for OpenAI Codex release
`rust-v0.153.4`. The manifest fixes GitHub release asset `545043499` for the
archive and release asset `545043543` for the bundle. The provisioner gets
these exact assets in the required build lane. It validates their names,
sizes, and SHA-256 digests before it puts them in the host-owned artifact
store. It then extracts and validates the AArch64 Linux ELF. Large release
assets are not Git objects. Manifest schema version 2 holds the release record,
the asset records, the extracted ELF, the Rekor entry identity, and the
retained evidence records. The provisioner, the integration test, and the CI
lanes read the release tag and the other identity values from this manifest.

Rekor supplied the entry for log index `2717156140` from its version-1
entries API. The fixture also retains the production trusted root, the exact
Rekor key, the signed checkpoint, and the inclusion proof. The test denies
network access before it calls production admission. It changes each retained
object and confirms that admission fails before adapter import.

## Release bump and rollback

The manifest `codex-sigstore-fixture.json` is the single source for the release
identity. A bump changes these records together:

- `release.repository`, `release.tag`, and `release.target`;
- each `assets` record: `id`, `api_url`, `download_url`, `name`, `size`, and
  `sha256`;
- `extracted_elf`: `name`, `size`, and `sha256`;
- `rekor`: `log_id`, `log_index`, and `integration_time`;
- each `retained` record when the trusted root, Rekor key, checkpoint, or proof
  changes.

After the manifest edit, refresh the retained evidence files in this directory.
Then run:

```bash
tag="$(python3 scripts/provision_codex_sigstore_fixture.py --print-tag)"
python3 scripts/provision_codex_sigstore_fixture.py \
    --root "$PWD/build/test-fixtures/codex-sigstore/$tag"
```

The provisioner checks the manifest records against the live GitHub release
metadata. It checks every retained file before it publishes the fixture. The
nightly `build` job derives the same tag, mounts the fixture read-only at
`/codex-sigstore/$tag`, and sets
`HEPHAESTUS_CODEX_SIGSTORE_FIXTURE_ROOT=/codex-sigstore/$tag`.

A bump is not one scalar change. The Sigstore bundle, the Rekor checkpoint and
proof, and sometimes the trusted root change with the release. If the pinned
release or asset is removed, select the next release in the supported Codex
compatibility line and refresh all records.

To roll back, revert the manifest change and restore the retained evidence from
the previous reviewed commit. Do not keep a mixed manifest and evidence set.
Do not disable the artifact lane to hide a failed refresh.

If the Sigstore trusted root retires the pinned Rekor log, a manifest edit
cannot make the old evidence valid. Refresh the evidence and review the
trusted-root update.

The following table binds each retained Codex evidence object to its origin,
immutable revision or log identity, and upstream project license.

| Object | Origin | Revision or log identity | License |
| --- | --- | --- | --- |
| `codex-aarch64-unknown-linux-musl.sigstore.b64` | `https://github.com/openai/codex/releases/download/rust-v0.153.4/codex-aarch64-unknown-linux-musl.sigstore` | Codex commit `3d2ee51ca2d5db578f328aa75e20aa22c0197c9a`, asset `545043543` | `Apache-2.0` |
| `codex-production-trusted-root.json` | `https://tuf-repo-cdn.sigstore.dev/trusted_root.json` | sigstore-python v4.5.0 commit `181074f4dc11b7e85ef44556e25248ef14fcb554` | `Apache-2.0` |
| `codex-rekor.pub` | Rekor key from `https://tuf-repo-cdn.sigstore.dev/trusted_root.json` | sigstore-python v4.5.0 commit `181074f4dc11b7e85ef44556e25248ef14fcb554`; log ID `c0d23d6ad406973f9559f3ba2d1ca01f84147d8ffc5b8445c224f98b9591801d` | `Apache-2.0` |
| `codex-rekor.checkpoint` | `https://rekor.sigstore.dev/api/v1/log/entries?logIndex=2717156140` | log index `2717156140`; tree size `2624064470` | `Apache-2.0` |
| `codex-rekor.proof` | `https://rekor.sigstore.dev/api/v1/log/entries?logIndex=2717156140` | log index `2717156140`; tree size `2624064470` | `Apache-2.0` |
