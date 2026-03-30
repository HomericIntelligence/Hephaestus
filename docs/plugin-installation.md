# ProjectHephaestus Claude Code Plugin — Installation Guide

ProjectHephaestus ships as a Claude Code plugin in addition to a Python package. Installing the plugin gives any repository in your ecosystem access to the `hephaestus` skill set.

## What the Plugin Provides

| Skill | Invocation | Description |
|-------|-----------|-------------|
| advise | `/advise <task>` | Search team knowledge before starting work |
| learn | `/learn` | Save session learnings as a new skill |
| myrmidon-swarm | `/myrmidon-swarm <task>` | Hierarchical agent delegation with Opus/Sonnet/Haiku model tiers |
| repo-analyze | `/repo-analyze` | Comprehensive repository audit across 15 dimensions |
| repo-analyze-strict | `/repo-analyze-strict` | Ruthlessly thorough audit — starts at F, evidence required |
| repo-analyze-quick | `/repo-analyze-quick` | Fast health check focused on showstoppers |

## Installation

### From GitHub (recommended)

```bash
claude plugin install HomericIntelligence/ProjectHephaestus
```

### From a local clone

```bash
claude plugin install /path/to/ProjectHephaestus
```

## Enabling in a Project

After installing, enable the plugin in your project's `.claude/settings.json`:

```json
{
  "enabledPlugins": {
    "hephaestus@ProjectHephaestus": true
  }
}
```

## Verifying Installation

Check that the plugin appears in your project's enabled plugins:

```bash
cat .claude/settings.json
```

You should see `hephaestus@ProjectHephaestus` listed under `enabledPlugins`. Skills will then be available as both `/repo-analyze` and the fully-qualified `hephaestus:repo-analyze` form.

## Usage Examples

```
/advise implement retry logic with exponential backoff
/repo-analyze
/repo-analyze-strict
/repo-analyze-quick
/learn
/myrmidon-swarm refactor the authentication module
```

The fully-qualified form is useful when multiple plugins define a skill with the same name:

```
/hephaestus:repo-analyze
/hephaestus:advise implement a new config loader
```
