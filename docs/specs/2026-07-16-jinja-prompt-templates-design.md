# Jinja prompt templates and harness overrides

**Status:** Proposed

**Owner:** The prompt subsystem owner defined by
[CODEOWNERS](../../.github/CODEOWNERS).

**Review trigger:** Any change to `PromptCatalog`, packaged template layout,
override precedence, or prompt parity requirements.

**Maintained sources:** [`PromptCatalog`](../../hephaestus/prompts/catalog.py)
and the [packaged default templates](../../hephaestus/prompts/templates/default/).

## Goal

Move every built-in, agent-facing Hephaestus prompt and reusable prompt
fragment out of Python source into packaged Jinja2 templates.  A harness must
be able to replace the content of any default template or fragment without
copying unrelated defaults. The harness cannot replace the required
ASD-STE100 writing standard wrapper. Except for this wrapper, rendering the
packaged defaults for the same inputs must preserve the current prompt text
byte-for-byte.

## Scope

The migration covers all prompts used to instruct an external coding/review
agent: the existing `hephaestus.automation.prompts` builders, pipeline stage
prompts, audit/learn/PR-management prompts, and the GitHub tidy and fleet-sync
agent prompts.  A user-provided prompt file consumed by `agent_stage` remains
an input artifact, not a Hephaestus default template.

## Layout

```
hephaestus/prompts/
  templates/
    default/
      address_review/
      advise/
      audit/
      ci/
      fleet_sync/
      follow_up/
      implementation/
      learn/
      planning/
      pr_management/
      pr_review/
      tidy/
      shared/
      strict_rubrics/
```

Template names are stable, slash-separated relative paths ending in `.j2`.
Every file in a harness override root uses exactly the same relative name as
the packaged default it replaces.  `shared/` and `strict_rubrics/` are normal
template names, so a harness may override a fragment as well as a complete
prompt.

## Rendering contract

`PromptCatalog` is the sole loader and renderer.  It uses a Jinja environment
with `StrictUndefined`, disabled autoescape, disabled block whitespace
trimming, LF newlines, and preserved trailing newlines.  These settings make
missing variables fail closed and preserve existing prompt formatting.

Python owns dynamic/safety-sensitive values.  In particular, it creates the
per-render nonce and fully fenced untrusted-content blocks before passing them
to the template.  Templates never construct a fence around raw GitHub content.
The catalog accepts only safe, relative `.j2` paths.  Prompt builders own the
set of template names they render; an override cannot escape its selected
directory or cause arbitrary Python-source loading.

For each complete agent-direction payload, the catalog adds the packaged
ASD-STE100 writing standard directive after it resolves the template. It reads
this directive only from the packaged default tree. A harness cannot remove
or replace it. For `learn/learn.j2`, the catalog puts the writing directive
after the replaceable content so the packaged `/learn` command stays first.
Other complete payloads put the directive first. A harness remains responsible
for the command syntax in the content that it replaces.

A non-template agent direction uses `PromptCatalog.apply_writing_standard` at
its production boundary. The fixed Pi smoke probe stays exact because it tests
a required literal. A custom Pi smoke prompt uses this method.

## Override resolution

The prompt root has the established Hephaestus precedence:

1. optional explicit `--prompt-dir PATH` command option, when supplied;
2. packaged `templates/default` resources.

An override root must be an existing directory.  It is layered ahead of the
packaged default loader: an override file wins; a missing file falls through
to the packaged default. This precedence applies to replaceable prompt
content, but not to the packaged writing standard wrapper. A malformed
override, missing required variable, or invalid registered path is an error,
never a silent fallback. CLI entrypoints thread the selected catalog through
their orchestration context; library callers may construct and pass a catalog
directly.

## Compatibility and tests

Before deleting a Python literal, deterministic representative workloads are
captured as legacy parity fixtures.  The test suite covers complete prompts,
shared fragments, partial harness overrides, strict undefined values,
package-resource loading, and wheel/sdist inclusion.  The migration must add
coverage for every text-changing variant (provider, review iteration,
prior-review, nitpick, and fenced-content paths) before it is complete.

The parity fixture is a deliberate compatibility oracle, not a prose-quality
snapshot. The product requirement is exact instantiated prompt preservation,
except for required governance wrappers such as the ASD-STE100 directive.

## Non-goals

Except for the required governance wrapper, this change does not alter
replaceable prompt content. It does not relax untrusted-input fencing, change
parsing contracts, or introduce an external template registry. A harness
supplies local files and remains responsible for versioning its own overrides.
