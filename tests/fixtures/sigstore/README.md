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
