"""Shared host-verification library for Hephaestus pipeline stages.

The package owns the canonical command specs, safe paths, receipts, and
diagnostics. Library code imports nothing from ``hephaestus.automation``, so
the dependency direction stays library to product.
"""

from .diagnostic import (
    HOST_VERIFICATION_DIAGNOSTIC_MAX as HOST_VERIFICATION_DIAGNOSTIC_MAX,
    host_verification_failure_comment as host_verification_failure_comment,
    publish_host_verification_failure as publish_host_verification_failure,
    publish_stage_diagnostic as publish_stage_diagnostic,
)
from .paths import (
    safe_changed_paths as safe_changed_paths,
    safe_relative_path as safe_relative_path,
)
from .publication_spec import (
    FIRST_PUBLICATION_CHECK as FIRST_PUBLICATION_CHECK,
    FIRST_PUBLICATION_CHECK_ARGV as FIRST_PUBLICATION_CHECK_ARGV,
    HEPHAESTUS_REQUIRED_CHECK as HEPHAESTUS_REQUIRED_CHECK,
    HEPHAESTUS_REQUIRED_CHECK_ARGV as HEPHAESTUS_REQUIRED_CHECK_ARGV,
    HEPHAESTUS_REQUIRED_CHECK_TIMEOUT_S as HEPHAESTUS_REQUIRED_CHECK_TIMEOUT_S,
)
from .receipt import (
    HostVerificationReceipt as HostVerificationReceipt,
    parse_host_verification_receipt as parse_host_verification_receipt,
)
from .repository import (
    HEPHAESTUS_REPOSITORY as HEPHAESTUS_REPOSITORY,
    REBASE_STRUCTURAL_TEST as REBASE_STRUCTURAL_TEST,
    REBASE_STRUCTURAL_TEST_ARGV as REBASE_STRUCTURAL_TEST_ARGV,
    REBASE_STRUCTURAL_TEST_PATH as REBASE_STRUCTURAL_TEST_PATH,
    is_hephaestus_repository as is_hephaestus_repository,
)
from .specs import (
    EMPTY_HOST_VERIFICATION_BOOTSTRAP_JSON as EMPTY_HOST_VERIFICATION_BOOTSTRAP_JSON,
    EMPTY_HOST_VERIFICATIONS_JSON as EMPTY_HOST_VERIFICATIONS_JSON,
    HOST_VERIFICATION_SPECS as HOST_VERIFICATION_SPECS,
    HOST_VERIFICATION_WORKSPACE_KEY as HOST_VERIFICATION_WORKSPACE_KEY,
    PRE_PR_TEST as PRE_PR_TEST,
    PRE_PR_TEST_ARGV as PRE_PR_TEST_ARGV,
    HostVerificationSpec as HostVerificationSpec,
    focused_test_argv as focused_test_argv,
    spec_for as spec_for,
)
