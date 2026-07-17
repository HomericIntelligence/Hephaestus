# Jinja prompt templates and harness overrides

**Status:** Proposed

## Goal

Move every built-in, agent-facing Hephaestus prompt and reusable prompt
fragment out of Python source into packaged Jinja2 templates.  A harness must
be able to replace any default template or fragment without copying unrelated
defaults.  Rendering the packaged defaults for the same inputs must preserve
the current prompt text byte-for-byte.

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

## Override resolution

The prompt root has the established Hephaestus precedence:

1. optional explicit `--prompt-dir PATH` command option, when supplied;
2. packaged `templates/default` resources.

An override root must be an existing directory.  It is layered ahead of the
packaged default loader: an override file wins; a missing file falls through
to the packaged default.  A malformed override, missing required variable, or
invalid registered path is an error, never a silent fallback.  CLI entrypoints
thread the selected catalog through their orchestration context; library
callers may construct and pass a catalog directly.

## Compatibility and tests

Before deleting a Python literal, deterministic representative workloads are
captured as legacy parity fixtures.  The test suite covers complete prompts,
shared fragments, partial harness overrides, strict undefined values,
package-resource loading, and wheel/sdist inclusion.  The migration must add
coverage for every text-changing variant (provider, review iteration,
prior-review, nitpick, and fenced-content paths) before it is complete.

The parity fixture is a deliberate compatibility oracle, not a prose-quality
snapshot: the product requirement is exact instantiated prompt preservation.

## Non-goals

This change does not alter prompt content, relax untrusted-input fencing,
change parsing contracts, or introduce an external template registry.  A
harness supplies local files and remains responsible for versioning its own
overrides.
