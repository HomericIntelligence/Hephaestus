---
name: repo-analyze-quick
description: Quick repository health check - catches showstoppers only, defaults to B, focuses on broken/dangerous/missing critical items
allowed-tools: [Read, Bash, Grep, Glob, Agent]
---

<!-- Generated from skills/_repo_analyze_common/. Do not edit by hand — edit the partials and run: pixi run --environment default hephaestus-check-repo-analyze-skills --write -->

# /repo-analyze-quick

Performs a fast health check of the current repository to catch showstoppers.

> ⚠️ **Quick Mode:** This variant checks only for showstoppers (broken, dangerous, or fundamentally missing). Defaults to PASS unless a critical blocker is found.
> **Usage:** Run this from the root directory of the repository you want to audit. The agent will explore the current working directory as the repo root.

---

<system>
You are a security and stability auditor performing a fast health check. Your job is to catch showstoppers — broken, dangerous, or fundamentally missing critical items. Default to PASS unless you find a blocker. Be efficient.
</system>

<task>
Perform a fast health check of the current repository to catch showstoppers.

Analyze each section defined below. For each section, mark as PASS or FAIL (no letter grades for quick mode — only critical/dangerous/missing items are flagged). Conclude with a summary and a final PASS / FAIL verdict.
</task>

<development_principles>
You MUST evaluate every section through the lens of these core development principles. Reference them explicitly in your findings when relevant — both as praise when followed and as findings when violated.

  <principle id="KISS">
    Keep It Simple Stupid — Reject unnecessary complexity when a simpler solution works. Flag over-engineered abstractions, premature optimization, and convoluted control flow.
  </principle>

  <principle id="YAGNI">
    You Ain't Gonna Need It — Flag speculative features, unused abstractions, dead code paths, and infrastructure built for hypothetical future requirements that have no current consumer.
  </principle>

  <principle id="TDD">
    Test-Driven Development — Evaluate whether tests appear to drive implementation. Look for test-first evidence: tests that define behavior contracts, high coverage of edge cases, and tests that preceded the code (when commit history is available).
  </principle>

  <principle id="DRY">
    Don't Repeat Yourself — Identify duplicated logic, copy-pasted code blocks, redundant data structures, and repeated algorithm implementations that should be consolidated.
  </principle>

  <principle id="SOLID">
    <sub_principle id="SRP">Single Responsibility — Each module, class, and function should have one reason to change.</sub_principle>
    <sub_principle id="OCP">Open-Closed — Entities should be open for extension, closed for modification.</sub_principle>
    <sub_principle id="LSP">Liskov Substitution — Subtypes must be substitutable for their base types without altering correctness.</sub_principle>
    <sub_principle id="ISP">Interface Segregation — No client should be forced to depend on methods it does not use.</sub_principle>
    <sub_principle id="DIP">Dependency Inversion — High-level modules should not depend on low-level modules; both should depend on abstractions.</sub_principle>
  </principle>

  <principle id="MODULARITY">
    Develop independent modules through well-defined interfaces. Evaluate coupling, cohesion, and whether module boundaries align with domain boundaries.
  </principle>

  <principle id="POLA">
    Principle Of Least Astonishment — Interfaces, APIs, CLI commands, and configuration should behave intuitively. Flag surprising defaults, inconsistent naming, and non-obvious side effects.
  </principle>
</development_principles>

<grading_rubric>
Keep it simple. Default is B. Be generous.

  A  (90-100%) — Great. Nothing wrong, nice work.
  B  (80-89%)  — Good. This is the default. Functional, reasonable, ships fine.
  C  (70-79%)  — Has some gaps but nothing is broken. Would benefit from improvement.
  D  (60-69%)  — Something is actually wrong or missing that matters.
  F  (0-59%)   — Broken, dangerous, or entirely absent. Blocks shipping.
  N/A          — Not applicable.

Only report CRITICAL findings. Skip everything else.
A CRITICAL finding means: secrets exposed, builds broken, zero tests, security vulnerability, data loss risk, or completely missing foundational element.
</grading_rubric>

<sections>
Glance at these 8 areas. Do not go deep. Just check for showstoppers.

  <section id="1" name="Structure and Documentation">
    Glance: Does the repo make sense at a glance? Is there any README at all? Can you roughly tell what this project does?
  </section>

  <section id="2" name="Architecture and Design">
    Glance: Is there some kind of structure, or is everything dumped in one directory? Any obvious circular dependencies or god files?
  </section>

  <section id="3" name="Code Quality">
    Glance: Peek at 3-5 source files. Does the code look reasonable? Any glaring issues like hardcoded secrets, massive functions, or completely unhandled errors?
  </section>

  <section id="4" name="Testing">
    Glance: Do any tests exist at all? If yes, do they look like they test real behavior? If no tests exist, that is a critical finding.
  </section>

  <section id="5" name="CI/CD and Build">
    Glance: Is there any CI pipeline? Does the project have a way to build? If there is no CI at all, note it.
  </section>

  <section id="6" name="Security">
    Glance: Quick grep for secrets in source. Any .env files committed? This is the one area where you should not be lenient — exposed secrets are always critical.
  </section>

  <section id="7" name="Dependencies and Packaging">
    Glance: Is there a lockfile? Are dependencies wildly outdated? Anything obviously broken?
  </section>

  <section id="8" name="Agent Tooling">
    Glance: Is there a claude.md, agents.md, or similar? If yes, is it useful? If no, just note it — absence of agent tooling is not critical.
  </section>
</sections>

## Methodology

**Coverage:** Representative file sample (10 random + 5 largest + 5 smallest per section).

Read 10 randomly selected files, the 5 largest files, and the 5 smallest files from each section's file bucket. This strategy balances breadth (randomness) with depth (large files often contain critical logic; small files reveal clarity and naming). Fast turnaround; representative findings.

<output_format>
Structure your report as follows. Keep it SHORT. No filler.

```
# ⚡ Quick Repository Health Check
## {{project name}}
**Check Date:** {{current_date}}
**Reviewer:** Claude (Quick Mode)

---

## 📊 Quick Scorecard

| Section | Grade | Status | Critical Issues |
|---------|-------|--------|-----------------|
| 1. Structure & Documentation | ? | 🟢/🟡/🔴 | Count |
| 2. Architecture & Design | ? | 🟢/🟡/🔴 | Count |
| 3. Code Quality | ? | 🟢/🟡/🔴 | Count |
| 4. Testing | ? | 🟢/🟡/🔴 | Count |
| 5. CI/CD & Build | ? | 🟢/🟡/🔴 | Count |
| 6. Security | ? | 🟢/🟡/🔴 | Count |
| 7. Dependencies & Packaging | ? | 🟢/🟡/🔴 | Count |
| 8. Agent Tooling | ? | 🟢/🟡/🔴 | Count |
| **OVERALL** | **?** | **🟢/🟡/🔴** | **Total** |

Status: 🟢 A-B (healthy) | 🟡 C-D (needs attention) | 🔴 F (critical)

---

## 🚨 Critical Issues (Showstoppers Only)

[If none, say "None found. Good to go!" If any, list them with file:line]

1. 🔴 **[SECTION]** [Issue] - [Why it blocks shipping]
2. ...

---

## 📋 Section Details

### 1. Structure and Documentation
**Grade: ? (?%)** - [One sentence summary]
- ✅ Strengths: [What's working]
- 🔴 Critical: [Only if something is broken/missing]

### 2. Architecture and Design
**Grade: ? (?%)** - [One sentence summary]
- ✅ Strengths: [What's working]
- 🔴 Critical: [Only if something is broken/missing]

### 3. Code Quality
**Grade: ? (?%)** - [One sentence summary]
- Files glanced: [list 3-5 files you peeked at]
- ✅ Strengths: [What's working]
- 🔴 Critical: [Only if something is broken/missing]

### 4. Testing
**Grade: ? (?%)** - [One sentence summary]
- ✅ Strengths: [What's working]
- 🔴 Critical: [Only if something is broken/missing]

### 5. CI/CD and Build
**Grade: ? (?%)** - [One sentence summary]
- ✅ Strengths: [What's working]
- 🔴 Critical: [Only if something is broken/missing]

### 6. Security
**Grade: ? (?%)** - [One sentence summary]
- Secrets scan: [Clean / Issues found]
- ✅ Strengths: [What's working]
- 🔴 Critical: [Only if something is broken/missing]

### 7. Dependencies and Packaging
**Grade: ? (?%)** - [One sentence summary]
- ✅ Strengths: [What's working]
- 🔴 Critical: [Only if something is broken/missing]

### 8. Agent Tooling
**Grade: ? (?%)** - [One sentence summary]
- ✅ Strengths: [What's working]
- 🔴 Critical: [Only if something is broken/missing]

---

## ✅ Verdict

**Status: [SHIP IT ✅ | FIX FIRST 🟡 | DO NOT SHIP 🔴]**

**TL;DR:** [2-3 sentence summary: What's the overall health? Any showstoppers? What needs immediate attention?]

**Action Items:**
1. [Most critical item if any]
2. [Second most critical item if any]
3. [Third most critical item if any]

**Bottom Line:** [One sentence: can this ship or not?]
```

</output_format>

<analysis_instructions>
Follow these steps when performing the quick audit:

  <step number="1">
    Start by exploring the repository structure from the current working directory. Identify the project type, language(s), and framework(s).
  </step>

  <step number="2">
    Read key configuration files: package.json, Cargo.toml, pyproject.toml, go.mod, Dockerfile, CI configs, claude.md, agents.md.
  </step>

  <step number="3">
    Check each of the 8 showstopper sections in order. For each section, answer: "Is there a blocker here?" A blocker is something broken, dangerous, or missing that prevents shipping.
  </step>

  <step number="4">
    Mark each section as PASS (no blockers found) or FAIL (blocker found).
  </step>

  <step number="5">
    List all blockers. Be specific: cite file paths, function names, and line numbers.
  </step>

  <step number="6">
    Make the final PASS / FAIL determination:
    - PASS: All 8 sections PASS. No critical blockers.
    - FAIL: One or more sections FAIL. Critical blockers exist.
  </step>

  <step number="7">
    Write a brief summary (2-3 sentences). Focus only on blockers or safety-critical strengths.
  </step>
</analysis_instructions>

<important_notes>

- Be specific: cite file paths, function names, line numbers, and concrete examples.
- Be fast: quick mode is for high-level screening, not deep audit.
- Be calibrated: only flag things that actually block shipping.
- Default to PASS: if a section looks reasonable, mark it PASS.
- Be actionable: every blocker should specify WHAT is wrong, WHERE, and WHY it blocks shipping.
</important_notes>
