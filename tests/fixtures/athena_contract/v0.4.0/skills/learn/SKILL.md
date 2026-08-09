---
name: learn
description: Preserve a verified, non-duplicate Mnemosyne lesson through an isolated-worktree pull request when requested; otherwise report without mutation. Fails closed if ~/.agent_brain/knowledge cannot be prepared.
argument-hint: <lesson or session summary>
allowed-tools: [Read, Write, Edit, Bash, Grep, Glob, Agent]
---

# Learn

Why: one general, canonical rule is more discoverable and safer than many session-specific copies.
First decide whether a durable delta exists; write a requested result through a reviewable PR.

## Prepare the knowledge repository

Prepare Mnemosyne at `$HOME/.agent_brain/knowledge` under the canonical
[`dependency-resolution` contract](../../docs/dependency-resolution.md). Report the resolved
repository, commit SHA, and trust basis. Any resolution, authentication, checkout, update, or
revalidation failure is blocking.

## Decide before writing

This phase is read-only.

1. Run `advise` with the proposed lesson.
2. Define retrieval intent as the trigger/context, desired outcome, constraints, and failure mode;
   never use title, issue number, or session wording as identity.
3. Search flat `skills/*.md` (not optional notes), group semantic matches by intent, and inspect each
   candidate and its Git history for provenance and prior consolidation.
4. Inspect every open PR in the resolved Mnemosyne repository: enumerate its changed flat
   `skills/*.md` artifacts and derive intent from their changed content. A title or path only finds a
   candidate; it is never sufficient duplicate evidence.
5. Record exactly one disposition before mutation:

   | Disposition | Use when | Action |
   | --- | --- | --- |
   | `amend` | One canonical entry has a material verified delta. | Update that entry only. |
   | `consolidate` | Two or more current entries share intent. | Select one canonical entry, merge non-superseded rules, and retire duplicates in the same PR. |
   | `create` | Intent is materially distinct. | Add one precisely named entry. |
   | `reject` | No durable, verified delta exists. | Report `no learnable change`; leave Mnemosyne unchanged. |
   | `blocked` | Provenance is uncertain, more than one open PR targets the selected canonical entry, the selected PR is not safely writable, or retirement is unsafe. | Leave Mnemosyne unchanged and request direction. |

Never evade a blocked consolidation by creating a near-duplicate. When exactly one open PR changes
the selected canonical entry, it is the delivery target: enter Existing-PR mode and incorporate the
verified delta there. Never create a competing PR. Stop rather than guessing when multiple open PRs
target that entry. Do not report `learn` complete after `reject` or `blocked`.

Generalize the smallest reusable decision rule. Keep task-specific facts only when another agent
needs them to execute or verify that rule. Preserve history in Git, not duplicate active entries.
Repository audits belong in `repo-review`; PR audits belong in `pr-review`; review depth is a mode.

## Privacy and proprietary-information gate

Treat the session, its repositories, and all discovery output as sensitive source material. A
durable lesson must capture only the general pattern, decision rule, and safely shareable evidence;
it must never store any of the following in a skill, its filename, frontmatter, examples, commit,
or PR description:

- PII or identifiers that can identify a person, account, customer, or organization;
- product, project, customer, vendor, or organization names and other non-public identifiers;
- internal paths, hostnames, URLs, repository names, issue IDs, environment names, or infrastructure
  details;
- proprietary source, configuration, prompts, logs, data, metrics, or operational details; or
- secrets, credentials, tokens, or other access material.

Replace sensitive specifics with a faithful general pattern (for example, "an isolated checkout"
instead of a local path). When public information provides an equivalent, cite or describe that
public equivalent rather than copying internal evidence. Never invent a public analogue, a result,
or verification evidence. If the lesson cannot be made useful without disclosing sensitive or
proprietary information, select `reject`, leave Mnemosyne unchanged, and report that no safe
learnable change exists.

If a lesson requires Athena implementation, complete that normal development first. Follow
[`development.md`](../../docs/policies/development.md): keep helpers in `skills/<name>/scripts/`,
add behavior-based executable tests under `tests/unit/`, and do not add inline executable Markdown,
wording tests, or non-consumed artifacts merely to support a lesson.

## Scope

Read-only discovery does not expand the requested scope. When the task requests durable learning,
the resolved repository and full delivery path are constructive work that may proceed through either
a new PR or the single Existing-PR target selected during discovery. A recommendation or indirect
invocation remains read-only; return the proposed repository, base, branch, files, and PR target.

## Existing-PR mode

Use this mode when discovery identifies exactly one open PR that changes the selected canonical
entry. Re-fetch and bind its canonical repository, URL/number, `OPEN` state, source repository/ref,
and head OID before editing. Create an isolated worktree on that source ref at the bound head OID,
verify its `HEAD`, and never modify the shared checkout or default branch.

Immediately before publishing, re-fetch the same identity and head. Push only to the bound PR source
ref, using the provider's safe expected-head/lease protection. If the ref moves, the source repository
is not safely writable, or any binding differs, preserve the worktree and stop. Do not create a
branch, open another PR, or retarget the change. Use the disposition-specific write allowlist below.

## Coordinate safely

When available, partition independent discovery, overlap analysis, drafting, and verification into
bounded work items; otherwise perform them sequentially without weakening evidence. New-PR writers
use isolated worktrees from the same resolved default-branch SHA; Existing-PR writers use only the
bound PR head. Give writers non-overlapping ownership; read-only work items never edit. The
coordinator owns each canonical entry or assigns one integration owner, rejects unrelated edits, runs
focused validation after each integration and complete relevant validation after the combined result,
and alone commits, pushes, and opens a new PR when applicable. Stop on ownership overlap, base drift,
or unexpected scope.

Without native isolation, use the installed `../git-worktrees/scripts/prepare_worktree.py` by absolute
path only for new-PR work: retain the resolved checkout as the current directory; use branch
`skill/<slug>`, `--path $HOME/.agent_brain/worktrees/knowledge-<slug>`,
`--path-root $HOME/.agent_brain/worktrees`, and `--start-point <resolved-default-SHA>`. Never use
this fallback to reconstruct an Existing-PR worktree.

## Deliver a requested change

1. Never modify the shared checkout. For a new PR, derive `slug` and `name` from lowercase ASCII
   letters, digits, and single hyphens using `[a-z0-9][a-z0-9-]*`; reject empty, control, `/`, `..`,
   and leading `-` values. Add a collision-resistant suffix when needed. Create `skill/<slug>` at
   `$HOME/.agent_brain/worktrees/knowledge-<slug>` from the resolved default-branch SHA; resolve the
   path first, require it directly below `$HOME/.agent_brain/worktrees`, and reject symlinked parents
   or destinations. This is the new-PR path for `create` and `consolidate`, not Existing-PR mode.
2. Before editing, resolve a closed, disposition-specific write allowlist of exact repository-relative
   paths:

   | Disposition | Allowed paths |
   | --- | --- |
   | `amend` | The resolved canonical entry only. |
   | `create` | One new `skills/<name>.md` entry only. |
   | `consolidate` | The canonical entry, each named duplicate to retire, and each verified active consumer that must migrate. |

   A template- or schema-required companion is allowed only when named in this list. Optional
   `.notes.md` evidence needs a current consumer. Do not discover new write paths while editing.
3. For `create`, read the resolved Mnemosyne template, schema, and validation rules before drafting.
   Use every required frontmatter field, including `name`, `description`, `category`, `date`, and
   `version`, plus the required section structure. Include searchable intent, verification,
   generalized use and workflow, relevant failed approaches, parameters, and evidence; omit unused
   session transcript detail.
4. Apply the selected disposition inside its allowlist. Amend only the canonical entry. During
   consolidation, migrate verified active consumers before retiring every named duplicate.
5. Before committing, review every proposed artifact and delivery text against the privacy and
   proprietary-information gate. Remove or generalize sensitive specifics; use a faithful public
   equivalent only when one exists. If safe generalization is not possible, reject the lesson.
6. Run Mnemosyne's relevant complete validation. Verify exactly one active entry remains for the
   intent and no duplicate intent or stale consolidated name was introduced.
7. Sign and DCO-attest the commit. For a new PR, push the feature branch and open a PR against the
   resolved default branch. For Existing-PR mode, push only to the already bound source ref and do
   not open another PR. Never auto-merge.
8. Report the disposition, bound or new PR URL, any retired entries, and exact validation evidence.

A write disposition succeeds only with its PR URL. If validation, push, or PR creation fails, preserve
the isolated worktree and report the blocker; never fall back to Athena, a default branch, or another
repository. Preserve delegated and delivery worktrees until their unique work is integrated or
explicitly rejected. Cleanup is separate: remove only worktrees created by this invocation, only with
user authority, only after confirming no uncommitted or unintegrated state remains. Otherwise report
each worktree's path, owner, revision, cleanliness, and integration state and leave it intact. Never
delete branches, discard changes, force removal, or touch a pre-existing worktree.
