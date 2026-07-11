"""GitHub API utilities using gh CLI.

This package preserves the import and patch surface of the former
``hephaestus.automation.github_api`` module while splitting responsibilities
across focused submodules. Submodules resolve patchable collaborators through
this package namespace at call time so existing ``mock.patch("...github_api.X")``
seams keep working.
"""

from __future__ import annotations

import logging

from hephaestus.github import client as _gh_client
from hephaestus.github.client import (
    ClaudeUsageCapError as ClaudeUsageCapError,
    GitHubRateLimitError as GitHubRateLimitError,
    GitHubUnavailableError as GitHubUnavailableError,
    gh_call as gh_call,
    gh_cli_timeout as gh_cli_timeout,
)
from hephaestus.github.rate_limit import (
    gh_rate_limit_reset_epoch as gh_rate_limit_reset_epoch,
)
from hephaestus.io.utils import write_secure as write_secure
from hephaestus.utils.cache import ThreadSafeCache as ThreadSafeCache
from hephaestus.utils.helpers import strip_null_bytes as strip_null_bytes

from ..git_utils import get_repo_info as get_repo_info, run as run
from ..models import IssueInfo as IssueInfo, IssueState as IssueState
from ..state_labels import STATE_SKIP as STATE_SKIP, is_skipped as is_skipped

logger = logging.getLogger(__name__)

# Patch seams for imported-through names. Submodules call these through the
# package object so package-level test patches keep reaching internal callers.
_gh_call = gh_call
_GH_BREAKER = _gh_client._GH_BREAKER
_GH_THROTTLE = _gh_client._GH_THROTTLE
io_write_secure = write_secure

# Mutable module state from the former flat module. Submodules read and write
# through this package object so rebinding remains process-wide.
# Repo-slug-keyed, lock-protected label cache (#1858). The former bare
# `set[str] | None` module global was neither repo-keyed nor locked, so the
# multithreaded pipeline coordinator let one repo's labels mask another's,
# dropping durable state:* label writes. Mirrors git_utils._repo_info_cache.
_label_cache: ThreadSafeCache[str, set[str]] = ThreadSafeCache()
_issue_state_cache: dict[int, IssueState] = {}

from .checks import (  # noqa: E402
    _is_gh_pr_checks_no_checks_error as _is_gh_pr_checks_no_checks_error,
    _map_pr_check as _map_pr_check,
    gh_pr_checks as gh_pr_checks,
)
from .diff import (  # noqa: E402
    _filter_comments_to_diff as _filter_comments_to_diff,
    _valid_review_positions as _valid_review_positions,
)
from .issue_states import (  # noqa: E402
    _fetch_batch_states as _fetch_batch_states,
    prefetch_issue_states as prefetch_issue_states,
)
from .issues import (  # noqa: E402
    _assert_body_has_closes as _assert_body_has_closes,
    _body_file as _body_file,
    _check_graphql_errors as _check_graphql_errors,
    _fetch_issue_comment_ids as _fetch_issue_comment_ids,
    _parse_issue_number as _parse_issue_number,
    fetch_issue_info as fetch_issue_info,
    gh_issue_comment as gh_issue_comment,
    gh_issue_create as gh_issue_create,
    gh_issue_delete_comment as gh_issue_delete_comment,
    gh_issue_json as gh_issue_json,
    gh_issue_upsert_comment as gh_issue_upsert_comment,
    gh_list_open_issues as gh_list_open_issues,
    is_issue_closed as is_issue_closed,
    parse_issue_dependencies as parse_issue_dependencies,
)
from .labels import (  # noqa: E402
    _ensure_labels_exist as _ensure_labels_exist,
    gh_create_label as gh_create_label,
    gh_issue_add_labels as gh_issue_add_labels,
    gh_issue_remove_labels as gh_issue_remove_labels,
    gh_list_labels as gh_list_labels,
    skip_epics as skip_epics,
)
from .prs import (  # noqa: E402
    OpenPrDiscoveryIncompleteError as OpenPrDiscoveryIncompleteError,
    _assert_branch_commits_signed as _assert_branch_commits_signed,
    _find_open_pr_for_head as _find_open_pr_for_head,
    _find_open_prs_for_head as _find_open_prs_for_head,
    _gh_commit_is_verified as _gh_commit_is_verified,
    _select_open_pr_for_base as _select_open_pr_for_base,
    fetch_open_prs as fetch_open_prs,
    gh_current_login as gh_current_login,
    gh_pr_create as gh_pr_create,
    gh_pr_label_names as gh_pr_label_names,
    gh_pr_state as gh_pr_state,
)
from .reviews import (  # noqa: E402
    ReviewCommentIndexKey as ReviewCommentIndexKey,
    _edit_or_keep_comments as _edit_or_keep_comments,
    _fetch_pr_inline_review_thread_nodes as _fetch_pr_inline_review_thread_nodes,
    _normalize_review_comment_body as _normalize_review_comment_body,
    _post_shadow_review_comment as _post_shadow_review_comment,
    _review_comment_already_covers as _review_comment_already_covers,
    _review_comment_keyword_tokens as _review_comment_keyword_tokens,
    _review_threads_for_review as _review_threads_for_review,
    gh_pr_inline_comment_index as gh_pr_inline_comment_index,
    gh_pr_review_post as gh_pr_review_post,
    gh_pr_update_review_comment as gh_pr_update_review_comment,
    gh_pr_wont_fix_line_index as gh_pr_wont_fix_line_index,
)
from .threads import (  # noqa: E402
    gh_pr_list_unresolved_threads as gh_pr_list_unresolved_threads,
    gh_pr_resolve_thread as gh_pr_resolve_thread,
)

__all__ = [
    "STATE_SKIP",
    "ClaudeUsageCapError",
    "GitHubRateLimitError",
    "GitHubUnavailableError",
    "IssueInfo",
    "IssueState",
    "OpenPrDiscoveryIncompleteError",
    "ReviewCommentIndexKey",
    "_assert_body_has_closes",
    "_assert_branch_commits_signed",
    "_body_file",
    "_check_graphql_errors",
    "_edit_or_keep_comments",
    "_ensure_labels_exist",
    "_fetch_batch_states",
    "_fetch_issue_comment_ids",
    "_fetch_pr_inline_review_thread_nodes",
    "_filter_comments_to_diff",
    "_find_open_pr_for_head",
    "_find_open_prs_for_head",
    "_gh_call",
    "_gh_commit_is_verified",
    "_is_gh_pr_checks_no_checks_error",
    "_map_pr_check",
    "_normalize_review_comment_body",
    "_parse_issue_number",
    "_post_shadow_review_comment",
    "_review_comment_already_covers",
    "_review_comment_keyword_tokens",
    "_review_threads_for_review",
    "_select_open_pr_for_base",
    "_valid_review_positions",
    "fetch_issue_info",
    "fetch_open_prs",
    "get_repo_info",
    "gh_call",
    "gh_cli_timeout",
    "gh_create_label",
    "gh_current_login",
    "gh_issue_add_labels",
    "gh_issue_comment",
    "gh_issue_create",
    "gh_issue_delete_comment",
    "gh_issue_json",
    "gh_issue_remove_labels",
    "gh_issue_upsert_comment",
    "gh_list_labels",
    "gh_list_open_issues",
    "gh_pr_checks",
    "gh_pr_create",
    "gh_pr_inline_comment_index",
    "gh_pr_label_names",
    "gh_pr_list_unresolved_threads",
    "gh_pr_resolve_thread",
    "gh_pr_review_post",
    "gh_pr_state",
    "gh_pr_update_review_comment",
    "gh_pr_wont_fix_line_index",
    "gh_rate_limit_reset_epoch",
    "is_issue_closed",
    "is_skipped",
    "parse_issue_dependencies",
    "prefetch_issue_states",
    "run",
    "skip_epics",
    "strip_null_bytes",
]
