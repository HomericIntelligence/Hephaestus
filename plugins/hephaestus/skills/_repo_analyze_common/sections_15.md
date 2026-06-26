<audit_sections>

  <!-- ============================================================ -->
  <!-- SECTION 1: PROJECT STRUCTURE & ORGANIZATION                   -->
  <!-- ============================================================ -->
  <section id="1" name="Project Structure and Organization">
    Evaluate the overall repository layout and organization.

    <criteria>
      - Logical directory structure that reflects domain boundaries (MODULARITY)
      - Separation of concerns: source, tests, docs, config, scripts in appropriate locations
      - Clean root directory — no clutter, sensible top-level files
      - Monorepo structure (if applicable): workspace configuration, shared packages
      - Consistent naming conventions for files, directories, and modules (POLA)
      - Appropriate use of index/barrel files without circular dependencies
      - No deeply nested directories that obscure discoverability (KISS)
    </criteria>
  </section>

  <!-- ============================================================ -->
  <!-- SECTION 2: DOCUMENTATION                                      -->
  <!-- ============================================================ -->
  <section id="2" name="Documentation">
    Evaluate all documentation artifacts for completeness, accuracy, and usefulness.

    <criteria>
      - README.md: project purpose, quick-start, prerequisites, installation, usage, contributing guide
      - CONTRIBUTING.md: coding standards, PR process, branch strategy
      - LICENSE file present and appropriate
      - Architecture decision records (ADRs) or design documents
      - API documentation (OpenAPI/Swagger specs, JSDoc, docstrings, etc.)
      - Inline code comments: meaningful, not redundant with code (KISS)
      - Runbook / operational documentation for deployment and incident response
      - Onboarding guide: can a new developer get productive within a day?
      - Documentation is up-to-date with the current state of the codebase
    </criteria>
  </section>

  <!-- ============================================================ -->
  <!-- SECTION 3: ARCHITECTURE & DESIGN                              -->
  <!-- ============================================================ -->
  <section id="3" name="Architecture and Design">
    Evaluate the system's architectural decisions, patterns, and structural integrity.

    <criteria>
      - Clear architectural pattern (layered, hexagonal, microservices, event-driven, etc.)
      - Separation of concerns between layers (SOLID/SRP, MODULARITY)
      - Dependency management: direction of dependencies, no circular deps (SOLID/DIP)
      - Appropriate use of design patterns — not over-patterned (KISS, YAGNI)
      - Domain modeling quality: entities, value objects, aggregates
      - Error handling strategy: consistent, informative, non-leaking
      - Configuration management: environment-based, secrets handling
      - Scalability considerations: statelessness, caching strategy, async patterns
      - Interface design: clean contracts between components (MODULARITY, POLA)
      - No premature abstraction or speculative generality (YAGNI)
      - Complexity proportional to problem being solved (KISS)
    </criteria>
  </section>

  <!-- ============================================================ -->
  <!-- SECTION 4: SOURCE CODE QUALITY                                -->
  <!-- ============================================================ -->
  <section id="4" name="Source Code Quality">
    Evaluate the implementation quality of the production source code.

    <criteria>
      - Code readability: clear naming, consistent style, self-documenting (POLA)
      - Function and method length — does each do one thing? (SOLID/SRP, KISS)
      - DRY compliance: no copy-pasted logic, shared utilities for common patterns (DRY)
      - Type safety: proper use of type systems, generics, null safety
      - Error handling: no swallowed exceptions, informative error messages
      - No dead code, commented-out blocks, or TODO/FIXME/HACK without tracking issues
      - Consistent code style enforced by linter/formatter configuration
      - Proper use of language idioms and standard library
      - No hardcoded values that should be configurable (magic numbers, URLs, credentials)
      - Immutability preferences where appropriate
      - Guard clauses and early returns over deep nesting (KISS)
      - Logging: structured, leveled, no sensitive data
    </criteria>
  </section>

  <!-- ============================================================ -->
  <!-- SECTION 5: TESTING                                            -->
  <!-- ============================================================ -->
  <section id="5" name="Testing">
    Evaluate the test suite for coverage, quality, and TDD evidence.

    <criteria>
      - Test presence: unit, integration, end-to-end, and/or contract tests
      - Test coverage: measured and reported (target varies by project criticality)
      - Test quality: tests assert behavior, not implementation details (TDD)
      - Test organization: mirrors source structure, clear naming, follows arrange-act-assert
      - Edge case coverage: null/empty inputs, boundaries, error paths, concurrency
      - Test isolation: no shared mutable state, no test order dependencies
      - Mocking strategy: appropriate use, not over-mocked (KISS)
      - Test data management: factories/fixtures, not hardcoded sprawling data
      - Performance/load tests where appropriate
      - Snapshot tests: justified, not used as a lazy substitute for proper assertions
      - Evidence of test-first development (TDD): tests define the contract, not just verify after the fact
      - No skipped or disabled tests without documented justification
      - Tests run fast enough to support developer workflow
    </criteria>
  </section>

  <!-- ============================================================ -->
  <!-- SECTION 6: CI/CD & BUILD PIPELINE                             -->
  <!-- ============================================================ -->
  <section id="6" name="CI/CD and Build Pipeline">
    Evaluate the continuous integration and deployment infrastructure.

    <criteria>
      - CI pipeline exists (GitHub Actions, GitLab CI, Jenkins, CircleCI, etc.)
      - Pipeline stages: lint → build → test → security scan → deploy
      - Build reproducibility: deterministic builds, lockfiles committed
      - Artifact management: versioned, stored, retrievable
      - Deployment strategy: blue-green, canary, rolling, or similar
      - Environment promotion: dev → staging → production with gates
      - Rollback capability documented and tested
      - Pipeline runs on every PR and merge to main
      - Build caching for performance
      - Branch protection rules enforced
      - Pipeline configuration is DRY — shared workflows/templates (DRY)
      - Secrets management in CI: no hardcoded tokens, uses vault/secrets manager
    </criteria>
  </section>

  <!-- ============================================================ -->
  <!-- SECTION 7: DEPENDENCY & PACKAGE MANAGEMENT                    -->
  <!-- ============================================================ -->
  <section id="7" name="Dependency and Package Management">
    Evaluate how external dependencies are managed.

    <criteria>
      - Lockfile present and committed (package-lock.json, yarn.lock, Cargo.lock, etc.)
      - Dependency versions pinned or range-constrained appropriately
      - No unnecessary dependencies — each one is justified (YAGNI)
      - No deprecated or unmaintained dependencies
      - Dependency audit: known vulnerabilities checked (npm audit, pip audit, etc.)
      - License compatibility: all dependency licenses compatible with project license
      - Dependency update strategy: Dependabot, Renovate, or manual cadence
      - Vendoring strategy (if applicable)
      - Separation of dev vs. production dependencies
      - No duplicate dependencies or competing libraries for the same purpose (DRY)
    </criteria>
  </section>

  <!-- ============================================================ -->
  <!-- SECTION 8: SECURITY                                           -->
  <!-- ============================================================ -->
  <section id="8" name="Security">
    Evaluate security posture across the codebase and infrastructure.

    <criteria>
      - No secrets, API keys, credentials, or PII in source code or commit history
      - Input validation and sanitization on all external inputs
      - Authentication and authorization: proper implementation, least privilege
      - OWASP Top 10 coverage: injection, XSS, CSRF, broken access control, etc.
      - Secure communication: TLS/HTTPS, certificate validation
      - SECURITY.md or vulnerability disclosure policy
      - Static Application Security Testing (SAST) integrated
      - Dependency vulnerability scanning (SCA) integrated
      - Secrets scanning in CI (e.g., truffleHog, git-secrets, gitleaks)
      - Rate limiting and abuse prevention where applicable
      - Data encryption at rest and in transit where applicable
      - Audit logging for security-relevant events
      - Container security (if applicable): minimal base images, non-root user, read-only fs
    </criteria>
  </section>

  <!-- ============================================================ -->
  <!-- SECTION 9: SAFETY & RELIABILITY                               -->
  <!-- ============================================================ -->
  <section id="9" name="Safety and Reliability">
    Evaluate operational safety, fault tolerance, and reliability engineering.

    <criteria>
      - Graceful degradation: system handles partial failures without cascading
      - Circuit breakers, retries with backoff, timeout configuration
      - Health checks and liveness/readiness probes
      - Monitoring and alerting: metrics, dashboards, on-call integration
      - Observability: distributed tracing, structured logging, correlation IDs
      - Data integrity protections: transactions, idempotency, validation
      - Backup and disaster recovery strategy
      - Chaos engineering or failure injection testing (if applicable)
      - Resource limits: memory, CPU, connections, thread pools
      - Graceful shutdown: drain connections, complete in-flight requests
      - SLA/SLO definitions with error budgets (if applicable)
    </criteria>
  </section>

  <!-- ============================================================ -->
  <!-- SECTION 10: PLANNING & PROJECT MANAGEMENT                     -->
  <!-- ============================================================ -->
  <section id="10" name="Planning and Project Management">
    Evaluate evidence of structured planning and project management practices.

    <criteria>
      - Roadmap or project plan visible (GitHub Projects, Jira, Linear, etc.)
      - Issue tracking: templates, labels, milestones, prioritization
      - PR/MR workflow: templates, review requirements, size guidelines
      - Git workflow: branching strategy documented (gitflow, trunk-based, etc.)
      - Commit message conventions: conventional commits or equivalent standard
      - Release management: versioning strategy (SemVer), release process documented
      - Technical debt tracking: labeled issues, prioritized backlog
      - Definition of Done for features/stories
      - Sprint/iteration cadence evidence (if applicable)
    </criteria>
  </section>

  <!-- ============================================================ -->
  <!-- SECTION 11: AI AGENT TOOLING & CONFIGURATION                  -->
  <!-- ============================================================ -->
  <section id="11" name="AI Agent Tooling and Configuration">
    Evaluate the repository's integration with AI-assisted development tools and agent systems.

    <criteria>
      - claude.md / CLAUDE.md presence: project context, coding conventions, architectural guidance for AI agents
      - agents.md / AGENTS.md presence: multi-agent coordination, role definitions, handoff protocols
      - Quality of agent configuration: is it specific, actionable, and up-to-date? (POLA)
      - Custom skills: defined skill files for domain-specific agent capabilities
      - MCP (Model Context Protocol) server configuration or integration
      - Hooks: pre/post command hooks for agent workflows (e.g., auto-lint, auto-test)
      - .cursorrules, .windsurfrules, or equivalent IDE agent configuration
      - AI-specific .gitignore patterns (agent workspace files, temporary outputs)
      - Agent memory / context management strategy
      - Guardrails: are agent permissions and boundaries clearly defined?
      - Agent tool definitions: well-scoped, documented, tested (SOLID/ISP, POLA)
      - Evidence of human-in-the-loop checkpoints for critical agent actions
      - Does agent configuration reflect the same development principles as the codebase? (KISS, YAGNI, DRY)
      - Prompt templates or system prompts versioned alongside code
    </criteria>
  </section>

  <!-- ============================================================ -->
  <!-- SECTION 12: PACKAGING & DISTRIBUTION                          -->
  <!-- ============================================================ -->
  <section id="12" name="Packaging and Distribution">
    Evaluate how the software is packaged and distributed to end users or consumers.

    <criteria>
      - Build output: clean, reproducible artifacts (binaries, containers, packages)
      - Containerization (if applicable): Dockerfile quality, multi-stage builds, minimal images
      - Package registry publishing: npm, PyPI, crates.io, Maven Central, etc.
      - Versioning automation: version bumps tied to releases
      - Install/upgrade documentation: clear steps for all supported platforms
      - Backwards compatibility policy documented
      - Migration guides for breaking changes
      - Distribution channels: documented and tested (POLA)
    </criteria>
  </section>

  <!-- ============================================================ -->
  <!-- SECTION 13: DEVELOPER EXPERIENCE (DX)                         -->
  <!-- ============================================================ -->
  <section id="13" name="Developer Experience">
    Evaluate how pleasant and productive it is to work in this codebase.

    <criteria>
      - One-command setup: can a new developer clone and run with minimal steps? (POLA)
      - Local development environment: Docker Compose, devcontainers, Makefile, or equivalent
      - Hot reload / fast feedback loops during development
      - Editor/IDE configuration: .editorconfig, recommended extensions, workspace settings
      - Debugging support: source maps, debug configurations, helpful error messages
      - Task runner or script organization: Makefile, package.json scripts, justfile, etc.
      - Pre-commit hooks: lint, format, type-check before commit
      - Consistent tooling: everyone uses the same versions (volta, nvm, asdf, mise, etc.)
      - Code generation or scaffolding tools for common patterns (DRY)
      - Clear error messages and helpful failure modes (POLA)
    </criteria>
  </section>

  <!-- ============================================================ -->
  <!-- SECTION 14: API DESIGN (if applicable)                        -->
  <!-- ============================================================ -->
  <section id="14" name="API Design">
    If the project exposes an API (REST, GraphQL, gRPC, CLI, SDK), evaluate its design quality. Mark N/A if not applicable.

    <criteria>
      - Consistent naming and URL conventions (POLA)
      - Proper HTTP methods and status codes (REST) or schema design (GraphQL)
      - Versioning strategy for backwards compatibility
      - Input validation with clear error responses
      - Pagination, filtering, and sorting for collection endpoints
      - Rate limiting and throttling
      - Authentication/authorization on all endpoints
      - API documentation: auto-generated from code or OpenAPI spec
      - SDK or client library provided (if applicable)
      - Idempotency for mutating operations (POLA)
      - HATEOAS or discoverability features (if REST)
      - No over-fetching or under-fetching patterns (KISS, ISP)
    </criteria>
  </section>

  <!-- ============================================================ -->
  <!-- SECTION 15: COMPLIANCE & GOVERNANCE                           -->
  <!-- ============================================================ -->
  <section id="15" name="Compliance and Governance">
    Evaluate regulatory, legal, and governance posture.

    <criteria>
      - License file present, correct, and compatible with dependencies
      - Code of Conduct (if open source)
      - GDPR / data privacy considerations documented (if handling personal data)
      - Accessibility compliance: WCAG standards (if user-facing)
      - Internationalization (i18n) readiness (if user-facing)
      - Audit trail for data changes
      - Data retention and deletion policies
      - Third-party service agreements and SLAs documented
    </criteria>
  </section>

</audit_sections>
