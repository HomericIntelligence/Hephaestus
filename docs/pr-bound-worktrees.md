# Pull-request-bound worktree preparation and publication

Two console scripts bind one repair to one observed pull request. They exist
so a workflow skill can repair a pull request without ever pushing an
unrelated or stale history over a reviewed head:

- `hephaestus-prepare-pr-worktree` creates a receipt-owned Git namespace, a
  worktree, and an immutable preparation receipt that binds the repository,
  pull request number, base, head, start point, and creation mode.
- `hephaestus-publish-pr-worktree` revalidates every live pull request field,
  rejects unrelated ancestry, and publishes with an exact expected-head lease.

Run either command with `--help` for the full option reference. Both scripts
accept `--json` and write one JSON outcome envelope on standard output on
every path, including argument errors detected after `--json` pre-detection.

## Command grammars

```text
hephaestus-prepare-pr-worktree
  PR_NUMBER_OR_URL
  --target-host github.com
  --target-repository OWNER/REPOSITORY
  --repository-root PATH
  --state-root PATH
  --worktree PATH
  --worktree-root PATH
  --branch LOCAL_BRANCH
  --mode {in-place,clean-rebuild,stacked}
  [--dependency-pr PR_NUMBER_OR_URL]
  [--gh-extra-path-root ROOT]
  [--gh-timeout SECONDS]
  [--gh-global-rate FLOAT]
  [--gh-global-burst FLOAT]
  [--json]
```

```text
hephaestus-publish-pr-worktree
  PR_NUMBER_OR_URL
  --target-host github.com
  --target-repository OWNER/REPOSITORY
  --state-root PATH
  --receipt PATH
  [--gh-extra-path-root ROOT]
  [--gh-timeout SECONDS]
  [--gh-global-rate FLOAT]
  [--gh-global-burst FLOAT]
  [--json]
```

Preparation rules:

- `--repository-root` is the read-only source checkout. Its `origin` must
  identify `--target-repository` as one canonical https GitHub URL.
- `--state-root` and `--worktree-root` must sit below
  `<repository-root>/build/`. Relative paths and symlink components are
  rejected.
- `--worktree` must be a new path below `--worktree-root`.
- `--branch` names a private local branch inside the namespace. It is never
  the remote destination.
- `--dependency-pr` is required only for `stacked` mode and rejected
  otherwise.
- `--gh-extra-path-root` admits only `ROOT/bin/gh` as the `gh` executable.
  The root must be absolute and must not escape through a symlink.

Publication rules:

- The target must match the receipt.
- `--receipt` must be a regular no-follow file below the exact
  `--state-root`.
- The worktree, namespace, local branch, remote repository, remote branch,
  and expected remote head come only from the receipt.
- The grammar has no caller-supplied destination branch or expected head.

## Exit classifications

| Exit | `outcome` | Meaning |
| --- | --- | --- |
| `0` | `prepared`, `published`, or `reconciled` | The command confirmed the result. |
| `1` | `rejected` | Contract validation failed, or readback confirmed a lease rejection. |
| `2` | `argument-error` | The command grammar is invalid. |
| `75` | `temporary-failure` | GitHub or Git failed before a possible write, or readback proved that a failed push did not change the head. |
| `76` | `unknown` | A push can have reached the server, but authoritative readback cannot determine the result. |
| `130` | `interrupted` | The operator stopped work before a possible write. Publication interruptions after a possible write use remote readback and return `0`, `75`, or `76`. |

With `--json`, the envelope carries `status`, `exit_code`, `outcome`,
`message`, and `receipt` on every path. Preparation adds the receipt path,
namespace, and worktree. Publication adds the candidate object and the
authoritative remote head when one was read.

## Start modes

| Mode | Start object | Required relationship |
| --- | --- | --- |
| `in-place` | Exact target `head.sha` | The target head is an ancestor of the result. |
| `clean-rebuild` | Exact target `base.sha` | The base is an ancestor of the result. The prior target head must not become an added ancestor when it differs from the base. |
| `stacked` | Exact dependency `head.sha` | The target base repository, branch, and object equal the dependency head repository, branch, and object. The prior target head must not become an added ancestor. |

The start object is fetched from its owner repository: the target head
repository for `in-place`, the target base repository for `clean-rebuild`,
and the dependency head repository for `stacked`. The fetch pins the recorded
object to a private ref, so later mode and ancestry checks run on the exact
observed history.

## Fork behavior

Base and head repository identities stay separate. The base repository owns
the target pull request endpoint and both censuses. Publication pushes to
`head.repo.full_name` through its canonical
`https://github.com/OWNER/REPOSITORY.git` URL. A pull request whose branch
lives in a fork therefore publishes to the fork, with the same exact lease.

## Preparation receipt

The receipt is a strict schema-version-1 JSON document named by the SHA-256
digest of its immutable fields. The digest is validated on every read, so a
tampered file is rejected before any use. Immutable fields:

- `schema_version`, `receipt_id`, and the immutable `prepared` state;
- `target`: host, target base repository, pull request number, canonical URL;
- `base`: repository, branch, and tip;
- `head`: repository, branch, and tip;
- `mode` and the exact start `repository`, `branch`, and `object`;
- `dependency`: the dependency pull request identity and its base and head
  data, or an explicit `null` value;
- `census`: the initial open-pull-request census;
- `source`: repository root, normalized origin, and configuration digest;
- `namespace`: state root path, directory identity, manifest digest, config
  digest, and permitted refs;
- `worktree`: worktree path, Git-directory identity, and local branch; and
- `expected_remote_head`.

The preparation receipt never changes after it is written. Publication
progress lives in a separate locked outcome record under
`<state-root>/outcomes/`. The outcome record states are `preflight-complete`,
`published`, `reconciled`, `temporary-failure`, `rejected`, and `unknown`.

## Census rules

Both preparation and publication read a strict open-pull-request census from
the base repository:

- The census requests explicit pages of 100 open pull requests and permits
  at most 100 pages.
- Each row must carry a valid number, open state, base repository, branch,
  and tip, and head repository, branch, and tip.
- Duplicate numbers inside one page, malformed rows, and a final page that is
  still full are rejected: a full final page prevents proof of complete
  traversal.
- The preparation census is stored in the receipt.
- During publication, a new census is read while the receipt lock is held,
  after the first target and dependency validation.

## Sibling ancestry rejection

While the receipt lock is held, publication fetches each live sibling head
from its recorded head repository into a receipt-owned private ref. The
candidate is compared with the union of the receipt-time and live sibling
heads. A sibling head that is an ancestor of the candidate is rejected unless
it is the declared `stacked` dependency. This is the control that stops a
repair built on another pull request's unrelated head from publishing.

The second census detects a sibling pull request that was created or changed
after preparation. The final target and dependency reads detect a base
branch, base tip, head branch, head tip, repository, state, or stack
relationship that changes while the census runs.

## Publication order

Inside the receipt lock, publication runs in this order before any push:

1. Validate the receipt path, schema, digest, state root, namespace manifest,
   worktree identity, and local branch.
2. Resolve one immutable candidate object from the worktree `HEAD`.
3. Validate the mode-specific start and ancestry relationship.
4. Compare the candidate with all receipt-time sibling heads.
5. Read the live target pull request from its base repository.
6. Compare its repository, number, URL, state, base repository, base branch,
   base tip, head repository, head branch, and head tip with the receipt.
7. For `stacked` mode, read and compare every dependency field.
8. Get and validate a new bounded open-pull-request census while the lock
   remains held.
9. Fetch each live sibling head into a private receipt-owned ref. Reject
   unauthorized ancestry before a push.
10. Record the immutable candidate and the completed local preflight state in
    the locked outcome record.
11. Read the target pull request again after all census and sibling
    operations.
12. Compare all target receipt fields again. Reject any repository, number,
    URL, state, base repository, base branch, base tip, head repository, head
    branch, or head tip change.
13. For `stacked` mode, read the dependency pull request again and compare
    all receipt-bound dependency fields and the exact
    target-base-to-dependency-head relationship.
14. Read the exact target branch from the canonical head repository. Require
    the receipt head tip.
15. Invoke the exact push through `run_git(pre_launch=...)`. The launch
    validator checks only the local namespace, candidate object, manifest,
    directory identities, and configuration.
16. Push the recorded object. Never push a mutable symbolic ref.

## Exact lease and readback

The push uses one exact expected-head lease:

```text
git push --force-with-lease=refs/heads/<head-branch>:<observed-head-oid> \
  https://github.com/<head-repository>.git \
  <result-oid>:refs/heads/<head-branch>
```

The lease is never `--force`. If the remote head moved, the server rejects
the push and the reviewed head is preserved. After the push, the exact head
branch is read back:

- The branch equals the result object: record `published` or `reconciled`,
  return `0`.
- A failed push leaves the expected old head: record `temporary-failure`,
  return `75`.
- The branch equals another object: record `rejected`, return `1`.
- Readback fails after a possible push: record `unknown`, return `76`, and do
  not retry.

## Unknown-result recovery

For a later publication call whose outcome record says `unknown`, the command
does readback before another push. It returns `reconciled` when the result is
present. A retry is permitted only when the final target and dependency
revalidation still succeeds and the head repository still shows the exact
expected old head.

There is no automatic rollback after confirmed publication. A new correction
must use a new receipt that binds the newly observed pull request head.

## The Myrmidon integration-root rule

One Myrmidon integration revision can produce only one integrated result.
Each independent pull request gets its own receipt, namespace, worktree,
integration root, and publication call. Dependency ancestry is admitted only
through explicit `stacked` mode, which requires `--dependency-pr` and the
exact target-base-to-dependency-head relationship. A repair that must combine
several pull requests cannot reuse one receipt or one namespace to do it.

## Related documentation

- [ADR-0066](adr/0066-pr-bound-repair-publication.md) records the
  architecture decision for this boundary.
- `hephaestus-prepare-worktree` remains the unrestricted start-point helper;
  it has no pull request publication authority.
