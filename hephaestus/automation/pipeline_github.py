"""Compose the queue-owned GitHub adapter."""

from .pipeline_github_audit import PipelineGitHubAuditReceipts
from .pipeline_github_check_policy import PipelineGitHubCheckPolicy
from .pipeline_github_mutations import PipelineGitHubMutations
from .pipeline_github_queries import PipelineGitHubQueries
from .pipeline_github_required_checks import PipelineGitHubRequiredChecks
from .pipeline_github_review_queries import PipelineGitHubReviewQueries
from .pipeline_github_reviews import PipelineGitHubReviews
from .pipeline_github_scope_expansion import PipelineGitHubScopeExpansion
from .pipeline_github_transport import PipelineGitHubTransport
from .state_labels import STATE_IMPLEMENTATION_GO, STATE_IMPLEMENTATION_NO_GO


class PipelineGitHub(
    PipelineGitHubTransport,
    PipelineGitHubQueries,
    PipelineGitHubCheckPolicy,
    PipelineGitHubRequiredChecks,
    PipelineGitHubReviewQueries,
    PipelineGitHubReviews,
    PipelineGitHubAuditReceipts,
    PipelineGitHubMutations,
    PipelineGitHubScopeExpansion,
):
    """Stable GitHub façade with explicit single-owner semantics.

    Coordinator contexts may cache an instance on the coordinator thread.
    Worker jobs construct a separate instance for each request; sharing an
    instance between threads is unsupported.
    """

    def mark_pr_implementation_go(self, pr_number: int) -> None:
        """Apply and read back exclusive ``state:implementation-go``."""
        if self._skip(f"mark PR #{pr_number} implementation-go"):
            return
        self._add_labels(pr_number, [STATE_IMPLEMENTATION_GO])
        self._remove_labels(pr_number, [STATE_IMPLEMENTATION_NO_GO])
        has_go, has_no_go = self.pr_has_implementation_state_label(pr_number)
        if not has_go or has_no_go:
            raise RuntimeError(f"PR #{pr_number} implementation-go label read-back failed")
