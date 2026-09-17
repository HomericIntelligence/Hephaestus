"""Compose the queue-owned GitHub adapter."""

from .pipeline.github_jobs import ReadRepositoryValidationCIRequest, RepositoryValidationCIRead
from .pipeline_github_audit import PipelineGitHubAuditReceipts
from .pipeline_github_check_policy import PipelineGitHubCheckPolicy
from .pipeline_github_mutations import PipelineGitHubMutations
from .pipeline_github_queries import PipelineGitHubQueries
from .pipeline_github_repository import PipelineGitHubRepositoryMetadata
from .pipeline_github_required_checks import PipelineGitHubRequiredChecks
from .pipeline_github_review_queries import PipelineGitHubReviewQueries
from .pipeline_github_review_validation import CometCIReader, collect_comet_ci
from .pipeline_github_reviews import PipelineGitHubReviews
from .pipeline_github_scope_expansion import PipelineGitHubScopeExpansion
from .pipeline_github_transport import PipelineGitHubTransport
from .state_labels import STATE_IMPLEMENTATION_GO, STATE_IMPLEMENTATION_NO_GO


class PipelineGitHub(
    PipelineGitHubTransport,
    PipelineGitHubQueries,
    PipelineGitHubRepositoryMetadata,
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

    def read_repository_validation_ci(
        self, request: ReadRepositoryValidationCIRequest
    ) -> RepositoryValidationCIRead:
        """Collect CI evidence inside the current worker operation deadline."""
        if request.repository.casefold() != f"{self.org}/{self.repo}".casefold():
            raise ValueError("The CI request repository does not match the accessor.")
        deadline = request.deadline_s
        if self._operation_deadline_s is not None:
            deadline = min(deadline, self._operation_deadline_s)
        reader = CometCIReader(deadline_s=deadline, shutdown=self._operation_shutdown)
        collection = collect_comet_ci(
            request.invocation, head_branch=request.head_branch, reader=reader
        )
        return RepositoryValidationCIRead(request, collection.receipts, collection.gaps)

    def mark_pr_implementation_go(self, pr_number: int) -> None:
        """Apply and read back exclusive ``state:implementation-go``."""
        if self._skip(f"mark PR #{pr_number} implementation-go"):
            return
        self._add_labels(pr_number, [STATE_IMPLEMENTATION_GO])
        self._remove_labels(pr_number, [STATE_IMPLEMENTATION_NO_GO])
        has_go, has_no_go = self.pr_has_implementation_state_label(pr_number)
        if not has_go or has_no_go:
            raise RuntimeError(f"PR #{pr_number} implementation-go label read-back failed")
