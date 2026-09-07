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
assets are not Git objects.

Rekor supplied the entry for log index `2717156140` from its version-1
entries API. The fixture also retains the production trusted root, the exact
Rekor key, the signed checkpoint, and the inclusion proof. The test denies
network access before it calls production admission. It changes each retained
object and confirms that admission fails before adapter import.

The following table binds each retained Codex evidence object to its origin,
immutable revision or log identity, and upstream project license.

| Object | Origin | Revision or log identity | License |
| --- | --- | --- | --- |
| `codex-aarch64-unknown-linux-musl.sigstore.b64` | `https://github.com/openai/codex/releases/download/rust-v0.153.4/codex-aarch64-unknown-linux-musl.sigstore` | Codex commit `3d2ee51ca2d5db578f328aa75e20aa22c0197c9a`, asset `545043543` | `Apache-2.0` |
| `codex-production-trusted-root.json` | `https://tuf-repo-cdn.sigstore.dev/trusted_root.json` | sigstore-python v4.5.0 commit `181074f4dc11b7e85ef44556e25248ef14fcb554` | `Apache-2.0` |
| `codex-rekor.pub` | Rekor key from `https://tuf-repo-cdn.sigstore.dev/trusted_root.json` | sigstore-python v4.5.0 commit `181074f4dc11b7e85ef44556e25248ef14fcb554`; log ID `c0d23d6ad406973f9559f3ba2d1ca01f84147d8ffc5b8445c224f98b9591801d` | `Apache-2.0` |
| `codex-rekor.checkpoint` | `https://rekor.sigstore.dev/api/v1/log/entries?logIndex=2717156140` | log index `2717156140`; tree size `2624064470` | `Apache-2.0` |
| `codex-rekor.proof` | `https://rekor.sigstore.dev/api/v1/log/entries?logIndex=2717156140` | log index `2717156140`; tree size `2624064470` | `Apache-2.0` |
