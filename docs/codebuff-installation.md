# ProjectHephaestus — Codebuff & Freebuff Installation Guide

ProjectHephaestus ships as a Codebuff agent plugin, providing all 23 skills as
invocable agents in [Codebuff](https://codebuff.com) and
[Freebuff](https://github.com/CodebuffAI/codebuff#freebuff-the-free-coding-agent).

> **For Claude Code or Codex**, see [plugin-installation.md](plugin-installation.md) instead.

## What the Plugin Provides

23 Codebuff agent definitions (`.agents/hephaestus-*.ts`), each wrapping a
Hephaestus skill. The agent reads its corresponding `skills/<name>/SKILL.md`
at runtime and follows the instructions.

## Prerequisites

- [Node.js](https://nodejs.org/) (v18+)
- Codebuff or Freebuff CLI installed:

```bash
npm install -g codebuff   # Paid, full model access
npm install -g freebuff   # Free, ad-supported, same agent system
```

## Installation

### Option 1: Clone from GitHub

```bash
git clone https://github.com/HomericIntelligence/ProjectHephaestus.git
cd ProjectHephaestus
```

Launch Codebuff or Freebuff from the project root — agents are available immediately.

### Option 2: Copy Agent Files to Your Project

```bash
HEPHAESTUS_REPO="/path/to/ProjectHephaestus"
cp "$HEPHAESTUS_REPO"/.agents/hephaestus-*.ts .agents/
cp -r "$HEPHAESTUS_REPO"/skills ./skills
cp "$HEPHAESTUS_REPO"/knowledge.md ./knowledge.md
```

### Option 3: Symlink (Recommended for Development)

```bash
HEPHAESTUS_REPO="/path/to/ProjectHephaestus"
mkdir -p .agents
for f in "$HEPHAESTUS_REPO"/.agents/hephaestus-*.ts; do
  ln -sf "$(realpath "$f")" ".agents/$(basename "$f")"
done
ln -sf "$(realpath "$HEPHAESTUS_REPO/skills")" ./skills
ln -sf "$(realpath "$HEPHAESTUS_REPO/knowledge.md")" ./knowledge.md
```

## Verification

```bash
codebuff  # or freebuff
# Then invoke:
hephaestus:skill-advisor I need to audit this repository
hephaestus:repo-analyze-quick
```

## Customization

Edit any `.agents/hephaestus-*.ts` to change the model or tools:

```typescript
model: 'anthropic/claude-opus-4',  // Use Opus for complex skills
toolNames: ['read_files', 'write_file', 'str_replace', 'run_terminal_command', 'spawn_agents', 'end_turn'],
```

## Relationship to Other Plugin Systems

| System | Config Location | Status |
|--------|----------------|--------|
| Claude Code | `.claude-plugin/`, `.claude/settings.json` | Supported |
| Codex | `.codex-plugin/`, `.agents/plugins/marketplace.json` | Supported |
| Codebuff / Freebuff | `.agents/hephaestus-*.ts`, `knowledge.md` | **This guide** |

All three systems share the same `skills/` directory as the source of truth.
