# This mixin consumes the adapter transport namespace by design.
# ruff: noqa: F403, F405
from .pipeline_github_contract import _PipelineGitHubHost
from .pipeline_github_transport import *


class PipelineGitHubMutations(_PipelineGitHubHost):
    """Own coordinator-approved non-review GitHub mutations."""

    def merge_pr_if_head(self, pr_number: int, reviewed_sha: str) -> ConditionalMergeResult:
        """Attempt one immediate squash merge conditional on the reviewed SHA.

        The request deliberately avoids the GitHub CLI PR-merge subcommand,
        native auto-merge, merge queues, administrator flags, and retries. A
        stage-owned lifecycle read decides whether an ambiguous request may be
        retried later.
        """
        if pr_number <= 0 or not re.fullmatch(r"[0-9a-fA-F]{40}", reviewed_sha):
            return ConditionalMergeResult(status=None, body=None, malformed=True)
        owner, name = self._owner_name()
        if self._skip(f"conditionally squash merge PR #{pr_number} at {reviewed_sha}"):
            return ConditionalMergeResult(status=None, body=None, dry_run=True)
        try:
            result = gh_call(
                [
                    "api",
                    "--method",
                    "PUT",
                    "--include",
                    f"/repos/{owner}/{name}/pulls/{pr_number}/merge",
                    "-f",
                    f"sha={reviewed_sha}",
                    "-f",
                    "merge_method=squash",
                ],
                check=False,
                retry_on_rate_limit=False,
                max_retries=1,
            )
        except (subprocess.SubprocessError, RuntimeError, OSError) as exc:
            logger.warning("PR #%s: conditional merge transport failure: %s", pr_number, exc)
            return ConditionalMergeResult(status=None, body=None, transport_error=True)
        status, body, malformed = _parse_included_http_response(result.stdout or "")
        if status is None:
            return ConditionalMergeResult(status=None, body=None, transport_error=True)
        return ConditionalMergeResult(status=status, body=body, malformed=malformed)

    def drive_green_learn_terminal(self, issue_number: int) -> bool:
        """Return True when the post-merge ``/learn`` is already terminal.

        Mirrors ``ci_driver.CIDriver._learn_record_terminal`` over the issue's
        arming record: captured/succeeded timestamps or a terminal
        ``learn_status`` mean ``/learn`` must never fire again (#848).
        """
        record = self._arming.load(issue_number) or {}
        if record.get("learn_captured_at") or record.get("learn_succeeded_at"):
            return True
        return str(record.get("learn_status") or "").lower() in {"succeeded", "failed"}

    def drive_green_learn_inflight(self, issue_number: int) -> bool:
        """Return whether a persisted /learn dispatch may already have run.

        A process can fail after the agent receives its prompt but before it
        writes its outcome. This durable claim is intentionally not treated as
        a successful result: recovery retains the record for inspection, but
        must never repeat the external learning side effect.
        """
        record = self._arming.load(issue_number) or {}
        return str(record.get("learn_status") or "").lower() == "in_progress"

    def add_labels(self, issue_number: int, labels: list[str]) -> None:
        """Durably add labels (``gh_issue_add_labels``)."""
        if self._skip(f"add labels {labels} to #{issue_number}"):
            return
        if self._repo_slug is not None:
            self._add_labels(issue_number, labels)
            return
        github_api.gh_issue_add_labels(issue_number, labels)

    def remove_labels(self, issue_number: int, labels: list[str]) -> None:
        """Durably remove labels (``gh_issue_remove_labels``)."""
        if self._skip(f"remove labels {labels} from #{issue_number}"):
            return
        if self._repo_slug is not None:
            self._remove_labels(issue_number, labels)
            return
        github_api.gh_issue_remove_labels(issue_number, labels)

    def edit_labels(self, issue_number: int, *, add: list[str], remove: list[str]) -> None:
        """Atomically add+remove labels in a single ``gh issue edit``."""
        if self._skip(f"edit labels on #{issue_number} (+{add} -{remove})"):
            return
        if self._repo_slug is not None:
            if add:
                existing = self._label_names()
                for label in add:
                    if label not in existing:
                        self._create_label(label)
                        existing.add(label)
        elif add:
            github_api._ensure_labels_exist(add)
        cmd = ["issue", "edit", str(issue_number)]
        for label in add:
            cmd.extend(["--add-label", label])
        for label in remove:
            cmd.extend(["--remove-label", label])
        if add or remove:
            (self._gh if self._repo_slug is not None else gh_call)(cmd)

    def close_issue_as_covered(self, issue_number: int, pr_number: int) -> None:
        """Close the issue as covered by a merged PR (``_review_utils``)."""
        if self._skip(f"close #{issue_number} as covered by PR #{pr_number}"):
            return
        if self._repo_slug is not None:
            self._gh(
                [
                    "issue",
                    "close",
                    str(issue_number),
                    "--comment",
                    f"Closed by merged PR #{pr_number} (Closes #{issue_number}).",
                ],
                check=False,
            )
            return
        close_issue_as_covered(issue_number, pr_number)

    def upsert_plan_comment(self, issue_number: int, body: str) -> None:
        """Upsert the actor-owned current plan, migrating the legacy heading key."""
        self.upsert_issue_comment(
            issue_number,
            PLAN_CANONICAL_MARKER,
            body,
            legacy_marker=PLAN_COMMENT_MARKER,
        )

    def upsert_issue_comment(
        self,
        issue_number: int,
        marker: str,
        body: str,
        *,
        legacy_marker: str | None = None,
    ) -> None:
        """Upsert one actor-owned canonical comment keyed on an opaque marker.

        Human-authored marker collisions are inert: they are neither trusted,
        patched, deleted, nor allowed to deny service. A legacy human-readable
        marker may be supplied only as an actor-owned migration candidate.
        """
        if self._skip(f"upsert {marker!r} comment on #{issue_number}"):
            return
        if not body.lstrip().startswith(marker):
            raise ValueError(f"canonical comment body must start with marker {marker!r}")
        comments = self._repo_issue_comments(issue_number)
        exact = [c for c in comments if str(c.get("body", "")).lstrip().startswith(marker)]
        owned = [comment for comment in exact if self._comment_owned_by_viewer(comment)]
        if not owned and legacy_marker is not None:
            owned = [
                comment
                for comment in comments
                if str(comment.get("body", "")).lstrip().startswith(legacy_marker)
                and self._comment_owned_by_viewer(comment)
            ]
        if not owned:
            self._post_issue_comment(issue_number, body)
            comments = self._repo_issue_comments(issue_number)
            owned = [
                comment
                for comment in comments
                if str(comment.get("body", "")).lstrip().startswith(marker)
                and self._comment_owned_by_viewer(comment)
            ]
            if not owned:
                # GitHub may be briefly read-after-write stale. The next
                # idempotent pass will discover and converge the new pointer.
                return

        target_id = owned[-1].get("databaseId")
        if target_id is None:
            raise RuntimeError(f"owned comment for {marker!r} has no database id")
        owner, name = (
            self._owner_name() if self._repo_slug is not None else github_api.get_repo_info()
        )
        if str(owned[-1].get("body", "")) != body:
            with github_api._body_file(body) as path:
                gh_call(
                    [
                        "api",
                        "--method",
                        "PATCH",
                        f"/repos/{owner}/{name}/issues/comments/{int(target_id)}",
                        "-F",
                        f"body=@{path}",
                    ]
                )
        for duplicate in owned[:-1]:
            duplicate_id = duplicate.get("databaseId")
            if duplicate_id is not None:
                self._delete_issue_comment(int(duplicate_id))

    def _post_issue_comment(self, issue_number: int, body: str) -> None:
        """Post one issue comment in the adapter's configured repository."""
        if self._repo_slug is not None:
            with github_api._body_file(body) as path:
                self._gh(["issue", "comment", str(issue_number), "--body-file", path])
            return
        github_api.gh_issue_comment(issue_number, body)

    def _delete_issue_comment(self, comment_id: int) -> None:
        """Delete one duplicate actor-owned comment in the configured repository."""
        owner, name = (
            self._owner_name() if self._repo_slug is not None else github_api.get_repo_info()
        )
        github_api.gh_issue_delete_comment(
            comment_id,
            repo=(owner, name),
            missing_ok=True,
        )

    def append_issue_comment(self, issue_number: int, marker: str, body: str) -> None:
        """Append an immutable actor-owned artifact once, failing on mismatched replay."""
        if self._skip(f"append immutable {marker!r} comment on #{issue_number}"):
            return
        if not body.lstrip().startswith(marker):
            raise ValueError(f"immutable comment body must start with marker {marker!r}")
        comments = self._repo_issue_comments(issue_number)
        matching = [
            comment
            for comment in comments
            if str(comment.get("body", "")).lstrip().startswith(marker)
            and self._comment_owned_by_viewer(comment)
        ]
        if matching:
            if any(str(comment.get("body", "")) != body for comment in matching):
                raise RuntimeError(f"immutable journal conflict for marker {marker!r}")
            # Immutable history is append-only. Identical actor-owned copies
            # can arise from a create race; tolerate them without rewriting or
            # deleting the durable audit trail.
            return
        self._post_issue_comment(issue_number, body)
        comments = self._repo_issue_comments(issue_number)
        matching = [
            comment
            for comment in comments
            if str(comment.get("body", "")).lstrip().startswith(marker)
            and self._comment_owned_by_viewer(comment)
        ]
        if any(str(comment.get("body", "")) != body for comment in matching):
            raise RuntimeError(f"immutable journal conflict for marker {marker!r}")

    def create_pr(self, issue_number: int, branch: str, title: str, body: str) -> int:
        """Durably ensure the PR exists and return its number (idempotent).

        PR creation requires a repo-scoped accessor.  The legacy helper can
        alter auto-merge state, so an unscoped caller must fail closed rather
        than delegate to it.

        First select and reuse an open PR on the supplied branch, then use
        ``find_pr_for_issue`` as the issue-level fallback before creating a
        PR with the *given* title/body — NOT ``pr_manager.ensure_pr_created``,
        which would discard the stage's composed body (protocol docstring).
        Dry-run returns 0 (no PR).
        """
        if self._repo_slug is None:
            raise RuntimeError("create PR requires a repo-scoped PipelineGitHub accessor")
        if self._repo_slug is not None:
            open_prs = self._open_prs_for_branch(branch)
            existing_on_branch = github_api._select_open_pr_for_base(open_prs, "main")
            if existing_on_branch is not None:
                return existing_on_branch
        existing = self.find_pr_for_issue(issue_number)
        if existing:
            return existing
        if self._skip(f"create PR for #{issue_number} from {branch!r}"):
            return 0
        if self._repo_slug is not None:
            github_api._assert_body_has_closes(body)
            github_api._assert_branch_commits_signed(branch, base="main")
            with github_api._body_file(body) as body_path:
                result = self._gh(
                    [
                        "pr",
                        "create",
                        "--head",
                        branch,
                        "--base",
                        "main",
                        "--title",
                        github_api.strip_null_bytes(title),
                        "--body-file",
                        body_path,
                    ]
                )
            raw_output = result.stdout
            output = raw_output.strip()
            match = re.search(r"/pull/(\d+)", output)
            if match:
                return int(match.group(1))
            logger.error("Failed to parse PR number from gh pr create output: %r", raw_output)
            raise RuntimeError(
                f"Failed to parse PR number from gh pr create output: {raw_output!r}"
            )
        return github_api.gh_pr_create(branch, title, body)

    def mark_pr_implementation_no_go(self, pr_number: int) -> None:
        """Apply and read back exclusive ``state:implementation-no-go``."""
        if self._skip(f"mark PR #{pr_number} implementation-no-go"):
            return
        if self._repo_slug is not None:
            self._add_labels(pr_number, [STATE_IMPLEMENTATION_NO_GO])
            self._remove_labels(pr_number, [STATE_IMPLEMENTATION_GO])
        else:
            pr_manager.mark_pr_implementation_no_go(pr_number)
        has_go, has_no_go = self.pr_has_implementation_state_label(pr_number)
        if has_go or not has_no_go:
            raise RuntimeError(f"PR #{pr_number} implementation-no-go label read-back failed")

    def claim_drive_green_learn(self, issue_number: int, pr_number: int) -> bool:
        """Persist and read back the pre-dispatch /learn claim.

        The claim is the exactly-once boundary for the agent's external
        learning work. A nonterminal arm record becomes ``in_progress``
        before the job is handed to the worker; a restart encountering that
        state must surface an unknown outcome instead of invoking /learn a
        second time.
        """
        if self._skip(f"claim drive-green learn for #{issue_number} (PR #{pr_number})"):
            return True
        # Hold a stable sibling lock across read/check/write/readback. The
        # JSON record is atomically replaced by save(), so it cannot itself be
        # the lock inode. Every coordinator process takes this same lock before
        # claiming, making only one external /learn dispatch possible.
        with file_lock(
            self._arming.learn_claim_lock_path(issue_number),
            require_exclusive=True,
        ):
            record = self._arming.load(issue_number) or {"pr_number": pr_number}
            status = str(record.get("learn_status") or "").lower()
            if status in {"succeeded", "failed", "in_progress"}:
                return False
            record["pr_number"] = pr_number
            record["learn_status"] = "in_progress"
            record["learn_attempted_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if not self._arming.save(issue_number, record):
                raise RuntimeError(
                    f"could not persist drive-green learn claim for issue #{issue_number}"
                )
            persisted = self._arming.load(issue_number)
            if (
                persisted is None
                or persisted.get("pr_number") != pr_number
                or persisted.get("learn_status") != "in_progress"
            ):
                raise RuntimeError(
                    f"could not verify drive-green learn claim for issue #{issue_number}"
                )
            return True

    def mark_drive_green_learn_result(self, issue_number: int, *, succeeded: bool) -> None:
        """Record the post-merge ``/learn`` outcome on the arming record.

        Mirrors ``post_merge_processor.mark_drive_green_learn_result`` (minus
        the session-evidence enrichment, which stays with the legacy driver
        until the cutover issue): written before FINISH_PASS so a restart can
        never replay ``/learn`` for the same merged PR.
        """
        if self._skip(f"record drive-green learn result for #{issue_number}"):
            return
        record = self._arming.load(issue_number) or {}
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        record["learn_attempted_at"] = timestamp
        if succeeded:
            record["learn_status"] = "succeeded"
            record["learn_succeeded_at"] = timestamp
            record["learn_captured_at"] = timestamp
        else:
            record["learn_status"] = "failed"
            record["learn_succeeded_at"] = None
            record["learn_captured_at"] = None
        if not self._arming.save(issue_number, record):
            raise RuntimeError(
                f"could not persist drive-green learn result for issue #{issue_number}"
            )
        persisted = self._arming.load(issue_number)
        if persisted is None or persisted.get("learn_status") != record["learn_status"]:
            raise RuntimeError(
                f"could not verify drive-green learn result for issue #{issue_number}"
            )

    def skip_epics(self, epics_labels: dict[int, list[str]]) -> None:
        """Tag epics ``state:skip`` via the sanctioned chokepoint.

        The ONE seeding write (doc row "Epic tagging is the one seeding
        write; done BEFORE excluding"), executed by the coordinator through
        ``github_api.skip_epics``.
        """
        if self._skip(f"tag epics {sorted(epics_labels)} {STATE_SKIP}"):
            return
        if self._repo_slug is not None:
            for number, labels in epics_labels.items():
                if STATE_SKIP not in labels:
                    self._add_labels(number, [STATE_SKIP])
                    try:
                        self.upsert_issue_comment(
                            number,
                            SKIP_REASON_MARKER,
                            format_skip_reason_comment(
                                "excluded from the planning loop as an epic/roadmap "
                                "tracking issue (checklist of child work, not a code task)"
                            ),
                        )
                    except Exception as exc:  # pragma: no cover - best-effort
                        logger.warning("could not post skip-reason comment on #%s: %s", number, exc)
            return
        github_api.skip_epics(epics_labels)

    def ensure_state_labels(self) -> None:
        """Ensure the ``state:*`` label vocabulary exists on the repo.

        Repo-stage step 1 [M] (doc section 1): idempotent
        ``_ensure_labels_exist`` over the full ``state_labels`` vocabulary.
        """
        # Keep provisioning driven by the one shared vocabulary.  In
        # particular, the orthogonal issue-work guard must be created without
        # accidentally joining the plan-state routing groups.
        wanted = list(STATE_LABEL_SPECS)
        if self._skip(f"ensure state labels exist: {wanted}"):
            return
        if self._repo_slug is not None:
            existing = self._label_names()
            for label in wanted:
                if label not in existing:
                    self._create_label(label)
            return
        github_api._ensure_labels_exist(wanted)
