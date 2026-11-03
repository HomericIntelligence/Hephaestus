# Privacy, Data Retention, and Deletion Policy

## Scope

Hephaestus is locally operated. The project runs no proxy, no telemetry
collector, no hosted database, and no model gateway.

Local operation does not mean that all processed data stays local. When an
operator selects Claude, Codex, GitHub, or a Pi provider, the selected command
line interface sends the selected data directly to that service. The operator
supplies the authentication.

## Data Inventory

The inventory is closed. A change to the tooling that adds a data category must
update this table in the same change.

| Data category | Local/project location | External recipient | Retention owner | Deletion path |
|---|---|---|---|---|
| Repository and GitHub content | Working tree; `build/.worktrees/`; `build/.issue_implementer/` | GitHub. Anthropic or OpenAI only when the content is part of a selected prompt. | Operator for local copies. GitHub or the provider under the selected account terms. | Delete local state. Delete GitHub content through GitHub. Use the provider controls for transmitted prompt data. |
| Selected agent prompts, responses, and session metadata | Process memory; automation logs and state; provider CLI user storage | Anthropic for Claude. OpenAI for Codex. | The selected provider, product, and account terms. | Clear the Hephaestus state. Use the provider deletion controls. |
| Credentials and account metadata | Operator environment variables; provider CLI credential stores; GitHub account | GitHub, Anthropic, OpenAI, or the Pi provider when a call uses the credential | The issuer of each credential | Revoke each credential at its issuer. See [Deletion Procedures](#deletion-procedures). |
| Automation state, worktrees, and logs | `build/` (git-ignored) | None | Operator | `rm -rf build/` |
| Pi smoke artifacts | A fresh `pi-smoke-*` directory below `--log-dir` | None. The provider receives only the bounded smoke prompt. | Operator | Delete the `pi-smoke-*` directory. |
| Crash bundles | `/tmp/crash-bundle/cores` by default, or the `--target-dir` value | None | Operator | `rm -rf /tmp/crash-bundle/cores`, or delete the `--target-dir` value. |
| Observability metrics | In-process Prometheus registry; local scrape endpoint only | None | Operator. The data lives only while the process runs. | Stop the process. |

## Retention

Hephaestus does not set the retention of data that it sends to a provider.
Claude Code and Codex use the account, product, and contract controls of the
selected service. Consumer, commercial, API, and enterprise accounts can keep
data for different periods.

Read the current provider guidance for each selected account:

- [Anthropic organization data retention](https://privacy.anthropic.com/en/articles/7996866-how-long-do-you-store-my-organization-s-data)
- [Anthropic consumer deletion guidance](https://privacy.anthropic.com/en/articles/7996878-can-you-delete-data-sent-via-claude-ai)
- [OpenAI API data controls](https://platform.openai.com/docs/models/default-usage-policies-by-endpoint)
- [OpenAI Codex CLI authentication guidance](https://help.openai.com/en/articles/11381614-api-codex-cli-and-sign-in-with-chatgpt)

The following local retention rules apply:

- **Credentials**: held in process memory for the life of a command. Hephaestus
  does not write a credential to disk, a log, or a state file. A provider CLI
  can keep its own credential store. Log formatters must not interpolate token
  values.
- **Automation state, worktrees, and logs** (`build/`): a cache of content whose
  system of record is GitHub. There is no fixed retention duty. Delete the
  directory at any time.
- **Crash bundles**: contain raw process memory and MUST be treated as
  secret-bearing. Review and delete a bundle within 30 days of capture. CI
  environments are ephemeral and destroy a bundle at teardown.
- **Pi smoke artifacts**: a record of one bounded smoke run. Delete the run
  directory when the investigation is complete.
- **Observability metrics**: live in the in-process registry and disappear when
  the process exits.
- **GitHub content**: GitHub governs retention on GitHub. The
  [GitHub Privacy Statement](https://docs.github.com/site-policy/privacy-policies/github-privacy-statement)
  and the repository owner settings apply.

## Deletion Procedures

- **Local caches and state**: run `rm -rf build/` from the repository root. Run
  `git worktree prune` after you remove `build/.worktrees/`.
- **Crash bundles**: run `rm -rf /tmp/crash-bundle/cores`, or delete the value
  of the operator `--target-dir` option.
- **Pi smoke artifacts**: delete the `pi-smoke-*` run directory below the
  operator `--log-dir`.
- **GitHub credentials**: revoke the token in the GitHub personal access token
  settings. Delete any local `gh` authentication with `gh auth logout`.
- **Anthropic credentials**: revoke the API key in the Anthropic console.
  Delete the Claude CLI credential store with the sign-out command of that CLI.
- **OpenAI credentials**: revoke the API key in the OpenAI platform. Delete the
  Codex CLI credential store with the sign-out command of that CLI.
- **Pi provider credentials**: revoke the credential with the operator-selected
  provider. Delete any local provider file that the operator supplied.
- **Environment variables**: clear the variable from the shell session and from
  the CI secret store. A value in shell history stays until the operator clears
  that history.
- **Content on GitHub**: delete comments, issues, and pull requests through the
  GitHub interface or API. GitHub applies its own data-deletion practices.

## Data Subject Requests (GDPR)

The project does not act as a data controller for third-party personal data. It
processes repository content under the GitHub authorization of the operator.

For a question, an access request, or an erasure request about personal data in
this repository's issues, pull requests, or history, email
**<research@villmow.us>**. Requests are acknowledged within 7 days.

A request about data that a provider received goes to that provider. Hephaestus
cannot delete provider-side data.

## External Processors and No Project Proxy

Hephaestus operates no proxy. Each selected command line interface sends data
directly to its provider and uses the operator-managed authentication for that
provider. The project controls no transmission path and no provider-side
retention.

| Processor | Data received | Direct transport and authentication | Retention and terms authority | Deletion route | Hephaestus proxy |
|---|---|---|---|---|---|
| GitHub | Repository content, issues, pull requests, reviews, labels, and check results | The GitHub API and the `gh` command line interface, with an operator GitHub token | GitHub, under the selected account and repository terms | Delete through GitHub. Revoke the token at GitHub. | No |
| Anthropic (Claude) | Each selected automation prompt: issue, pull request, diff, review, plan, and repository material in that prompt, plus the response and session metadata | The `claude` command line interface with operator-managed authentication | Anthropic, under the selected product and account controls | Use the Anthropic deletion controls. Revoke the key in the Anthropic console. | No |
| OpenAI (Codex) | Each selected automation prompt: issue, pull request, diff, review, plan, and repository material in that prompt, plus the response and session metadata | The `codex` command line interface with operator-managed authentication | OpenAI, under the selected product and account controls | Use the OpenAI data controls. Revoke the key in the OpenAI platform. | No |
| Operator-selected Pi provider | The bounded smoke prompt of `run_pi_smoke_session`, and the Pi automation prompt when a host registers a reviewed OS-isolation adapter | The `pi` command line interface with operator-managed authentication | The selected Pi provider, under its own terms | Use the provider deletion controls. Revoke the credential with the provider. | No |

Normal Pi automation is admission-gated. Hephaestus refuses a Pi automation run
that has no reviewed external OS-isolation adapter. Only the smoke path runs
without an adapter. The smoke path sends the fixed smoke prompt and carries no
automatic GitHub or repository context.
