# Contract Testing

The contract lane exercises the real external command-line boundaries used by
the automation layer:

- `hephaestus.github.client.gh_call` for authenticated, read-only GitHub calls.
- `hephaestus.automation.claude_invoke.invoke_claude_with_session` for a real
  Claude session create-and-resume flow.

The lane is opt-in and skipped by normal unit, integration, and required CI
runs. Run the GitHub contract checks with:

```bash
just test-contract
```

The recipe passes the registered `--run-contract-tests` pytest option. Ambient
environment variables do not enable collection.

The agent check spends model tokens and requires a separate opt-in:

```bash
uv run pytest tests/integration/contract/test_agent_contract.py \
  --run-contract-tests --run-contract-agent \
  --override-ini="addopts=" -v --strict-markers
```

Pytest options:

- `--run-contract-tests` enables collection of the contract marker.
- `--run-contract-agent` enables the real Claude invocation test.
- `--contract-repo OWNER/REPOSITORY` pins GitHub calls to a target
  repository. If omitted, the repository is resolved once from the checkout
  root with an explicit working directory.
- `--contract-model MODEL` selects the Claude model for the agent lane; the
  default is `haiku`.

The GitHub lane only calls read endpoints (`rate_limit`, `repo view`, and
`issue list`) through `gh_call`. Its negative case confirms a missing endpoint
fails promptly without retrying a deterministic 404. The agent lane uses
pytest's `tmp_path` as its working directory and a trivial prompt. The default
model is intentionally inexpensive. Both lanes first require the corresponding
CLI to be installed and authenticated; missing credentials produce skips, not
failures.

The repository also provides a manual-only `.github/workflows/contract.yml`
workflow. It supplies the workflow's read-only GitHub token and pins the target
repository to `github.repository`. The agent lane remains skipped there unless
the second token-cost opt-in is added deliberately.
