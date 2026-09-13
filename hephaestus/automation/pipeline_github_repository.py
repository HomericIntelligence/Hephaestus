"""Repository metadata reads for the queue-owned GitHub adapter."""

from .github_api.graphql import repository_default_branch_query
from .pipeline.merge_wait_admission import VerifiedRepositoryDefaultBranch
from .pipeline_github_contract import _PipelineGitHubHost


class PipelineGitHubRepositoryMetadata(_PipelineGitHubHost):
    """Read validated repository identity and default-branch metadata."""

    def verified_repository_default_branch(self) -> VerifiedRepositoryDefaultBranch:
        """Return exact repository metadata without a local branch fallback."""
        owner, name = self._owner_name()
        return self._graphql(repository_default_branch_query(owner, name))
