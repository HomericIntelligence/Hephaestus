---
name: brainstorm
description: Use before any creative work — creating features, building components, adding functionality, or modifying behavior. Explores user intent and requirements before implementation.
argument-hint: <idea or feature description>
allowed-tools: [Read, Write, Bash, Grep, Glob, Agent]
---

# Brainstorming Ideas Into Designs

Help turn ideas into fully formed designs and specs through natural collaborative dialogue.

Start by understanding the current project context, then ask questions one at a time to refine the idea. Once you understand what you're building, present the design and get user approval.

**HARD GATE:** Do NOT write any code, scaffold any project, or take any implementation action until you have presented a design and the user has approved it. This applies to EVERY request regardless of perceived simplicity.

## Anti-Pattern: "This Is Too Simple To Need A Design"

Every feature goes through this process. "Simple" projects are where unexamined assumptions cause the most wasted work. The design can be short (a few sentences), but you MUST present it and get approval.

## Checklist

Complete in order:

1. **Run `/hephaestus:advise`** with the feature description — check ProjectMnemosyne for prior learnings on this topic
2. **Explore project context** — check files, docs, recent commits
3. **Ask clarifying questions** — one at a time, understand purpose/constraints/success criteria
4. **Propose 2-3 approaches** — with trade-offs and your recommendation
5. **Present design** — in sections scaled to their complexity, get user approval after each section
6. **Write design doc** — save to `docs/specs/YYYY-MM-DD-<topic>-design.md` and commit
7. **Spec self-review** — scan for placeholders, contradictions, ambiguity, scope issues
8. **User reviews written spec** — ask user to review the spec file before proceeding
9. **Transition to implementation** — invoke the `planning` skill or `myrmidon-swarm` for implementation

## The Process

**Understanding the idea:**

- Check out the current project state first (files, docs, `git log --oneline -10`)
- Before asking detailed questions, assess scope: if the request describes multiple independent subsystems, flag this immediately. Help the user decompose into sub-projects first.
- For appropriately-scoped projects, ask questions one at a time
- Prefer multiple choice questions when possible
- Only one question per message
- Focus on: purpose, constraints, success criteria

**Exploring approaches:**

- Propose 2-3 different approaches with trade-offs
- Lead with your recommended option and explain why
- Reference existing patterns in the Hephaestus codebase

**Presenting the design:**

- Present in sections, ask after each whether it looks right
- Scale each section to its complexity
- Cover: architecture, components, data flow, error handling, testing strategy
- Follow Hephaestus principles: KISS, YAGNI, DRY, SOLID

**Working in existing codebases:**

- Follow existing patterns in `hephaestus/`
- Run `/advise` first to check for existing implementations
- Don't propose unrelated refactoring — stay focused on the current goal

## After the Design

**Write spec doc:**

- Save to `docs/specs/YYYY-MM-DD-<topic>-design.md`
- Commit: `docs(specs): add <topic> design document`

**Spec Self-Review:**

1. **Placeholder scan:** Any "TBD", "TODO", incomplete sections? Fix them.
2. **Internal consistency:** Do any sections contradict each other?
3. **Scope check:** Is this focused enough for a single plan?
4. **Ambiguity check:** Can any requirement be interpreted two ways? Pick one and make it explicit.

**User Review Gate:**
After the self-review:
> "Spec written and committed to `docs/specs/<filename>`. Please review and let me know if you want changes before we start planning implementation."

Wait for approval. Only proceed once approved.

**Implementation:**

- Invoke the `planning` skill for task tracking, or
- Invoke `/hephaestus:myrmidon-swarm` for complex multi-agent work

## Key Principles

- **One question at a time** — don't overwhelm with multiple questions
- **YAGNI ruthlessly** — remove unnecessary features from all designs
- **Explore alternatives** — always propose 2-3 approaches
- **Incremental validation** — present design sections, get approval before moving on
- **Run `/advise` first** — don't propose what's already been built or debugged

---

_Adapted from [obra/superpowers](https://github.com/obra/superpowers) under the [MIT License](https://github.com/obra/superpowers/blob/main/LICENSE). Copyright (c) 2025 Jesse Vincent._
