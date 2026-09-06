# ADR-0037: Version-bound Mnemosyne checkout

- Status: Accepted
- Date: 2026-09-04
- Tracks: Mnemosyne learning availability
- Supersedes: ADR-0025 checkout freshness requirements

## Context

ADR-0025 required the host to synchronize the Mnemosyne checkout and to match
its commit to a remote branch head. This requirement stopped learning when the
host had a clean and valid Mnemosyne release but could not authenticate, fetch,
or fast-forward. A commit match is a freshness check. It is not a compatibility
check for the installed Mnemosyne release.

The host must keep repository identity and local-content checks. However, a
temporary remote failure or an older local checkout must not make the available
release unusable.

## Decision

The Mnemosyne resolver selects a trusted repository and its default branch. It
does not resolve or bind a remote commit.

If remote resolution is unavailable, the host can use an existing canonical
checkout. Before it does this, it verifies the local Git worktree, safe
configuration, clean state, and exact canonical origin. The host does not use
this fallback for a fork because it cannot verify fork ancestry without remote
metadata.

For an existing checkout, the host continues to reject a symlink, a wrong
origin, unsafe Git configuration, a dirty worktree, an invalid commit, or
invalid project metadata. The host reads the declared `project-mnemosyne`
version from `pyproject.toml` at the local commit. This version is the binding
identity.

The host makes one safe synchronization attempt when synchronization is
enabled. It fetches `origin`, checks out the default branch, and uses a
fast-forward-only merge. An authentication, transport, checkout, or
fast-forward failure is not a binding failure. The receipt records whether the
update completed and uses the clean local version.

The receipt keeps the local commit SHA as provenance and as an immutable Git
object reference. It does not compare that SHA to a remote SHA. If no local
checkout exists, the host must still clone a trusted repository. A clone
failure is fatal because no available local version exists.

The repository target keeps its optional `head_sha` field for compatibility.
This field is informational only. The binding does not require it and does not
compare it with the local commit.

## Alternatives considered

- Require the checkout to match the current remote commit. Rejected because a
  remote commit is a freshness value, not a release compatibility value.
- Skip all synchronization. Rejected because a safe update is useful when the
  remote service is available.
- Rebase local commits on the remote branch. Rejected because a rebase can
  rewrite local history and can leave the shared checkout in a conflicted
  state.
- Accept dirty or untrusted checkouts. Rejected because availability does not
  remove the local trust boundary.

## Consequences

- Learning can use a clean installed Mnemosyne release during a remote outage
  or when the checkout is behind its default branch.
- Binding receipts identify the Mnemosyne release version and report the update
  status.
- Commit SHAs remain audit evidence. They are not freshness gates.
- Operators can see that a run used an available version that was not updated.
