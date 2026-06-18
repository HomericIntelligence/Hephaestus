#!/usr/bin/env python3
"""Generate Codebuff agent definitions for all Hephaestus skills."""
from pathlib import Path
AGENTS_DIR = Path(".agents")
SKILLS = [
    {"name": "skill-advisor", "display": "Skill Advisor", "desc": "Routes tasks to the correct Hephaestus skill before you begin.", "tools": ["read_files", "run_terminal_command", "end_turn"]},
    {"name": "advise", "display": "Advise", "desc": "Searches ProjectMnemosyne for prior learnings before starting work.", "tools": ["read_files", "run_terminal_command", "spawn_agents", "end_turn"]},
    {"name": "learn", "display": "Learn", "desc": "Captures session learnings as a skill in ProjectMnemosyne.", "tools": ["read_files", "write_file", "str_replace", "run_terminal_command", "spawn_agents", "end_turn"]},
    {"name": "myrmidon-swarm", "display": "Myrmidon Swarm", "desc": "Hierarchical agent delegation with Opus/Sonnet/Haiku model tiers.", "tools": ["read_files", "write_file", "str_replace", "run_terminal_command", "spawn_agents", "end_turn"]},
    {"name": "brainstorm", "display": "Brainstorm", "desc": "Turns ideas into designs through collaborative dialogue before implementation.", "tools": ["read_files", "write_file", "run_terminal_command", "spawn_agents", "end_turn"]},
    {"name": "test-driven-development", "display": "Test-Driven Development", "desc": "Enforces RED-GREEN-REFACTOR cycle.", "tools": ["read_files", "write_file", "str_replace", "run_terminal_command", "end_turn"]},
    {"name": "systematic-debugging", "display": "Systematic Debugging", "desc": "Requires root cause investigation before proposing fixes.", "tools": ["read_files", "write_file", "str_replace", "run_terminal_command", "end_turn"]},
    {"name": "verification", "display": "Verification", "desc": "Requires running verification commands before claiming work is complete.", "tools": ["read_files", "run_terminal_command", "end_turn"]},
    {"name": "git-worktrees", "display": "Git Worktrees", "desc": "Creates isolated git worktrees for feature work.", "tools": ["read_files", "run_terminal_command", "end_turn"]},
    {"name": "finish-branch", "display": "Finish Branch", "desc": "Guides branch completion with structured options for merge/PR/cleanup.", "tools": ["read_files", "run_terminal_command", "end_turn"]},
    {"name": "code-review", "display": "Code Review", "desc": "Dispatches a Sonnet code reviewer and guides reception of feedback.", "tools": ["read_files", "run_terminal_command", "spawn_agents", "end_turn"]},
    {"name": "repo-analyze", "display": "Repo Analyze", "desc": "Comprehensive 15-dimension repository audit with grading.", "tools": ["read_files", "run_terminal_command", "end_turn"]},
    {"name": "repo-analyze-quick", "display": "Repo Analyze Quick", "desc": "Quick repository health check.", "tools": ["read_files", "run_terminal_command", "end_turn"]},
    {"name": "repo-analyze-strict", "display": "Repo Analyze Strict", "desc": "Ruthlessly thorough repository audit with strict grading.", "tools": ["read_files", "run_terminal_command", "end_turn"]},
    {"name": "repo-analyze-full", "display": "Repo Analyze Full", "desc": "Full-coverage audit via swarm agents.", "tools": ["read_files", "run_terminal_command", "spawn_agents", "end_turn"]},
    {"name": "repo-analyze-quick-full", "display": "Repo Analyze Quick Full", "desc": "Quick health check with full file coverage.", "tools": ["read_files", "run_terminal_command", "spawn_agents", "end_turn"]},
    {"name": "repo-analyze-strict-full", "display": "Repo Analyze Strict Full", "desc": "Strict audit with full file coverage.", "tools": ["read_files", "run_terminal_command", "spawn_agents", "end_turn"]},
    {"name": "review-pr-strict", "display": "Review PR Strict", "desc": "Ruthlessly thorough PR alignment audit.", "tools": ["read_files", "run_terminal_command", "spawn_agents", "end_turn"]},
    {"name": "worktree-cleanup", "display": "Worktree Cleanup", "desc": "Audits and prunes git worktrees.", "tools": ["read_files", "run_terminal_command", "end_turn"]},
    {"name": "tidy", "display": "Tidy", "desc": "Rebases all local branches with swarm conflict resolution.", "tools": ["read_files", "run_terminal_command", "end_turn"]},
    {"name": "create-reusable-utilities", "display": "Create Reusable Utilities", "desc": "Ports and generalizes utility scripts.", "tools": ["read_files", "write_file", "str_replace", "run_terminal_command", "end_turn"]},
    {"name": "github-actions-python-cicd", "display": "GitHub Actions Python CI/CD", "desc": "Sets up GitHub Actions CI/CD for Python projects.", "tools": ["read_files", "write_file", "str_replace", "run_terminal_command", "end_turn"]},
    {"name": "python-repo-modernization", "display": "Python Repo Modernization", "desc": "Brings a Python repo to production-grade quality.", "tools": ["read_files", "write_file", "str_replace", "run_terminal_command", "end_turn"]},
]
def generate_agent(skill):
    name = skill["name"]
    tools_str = ", ".join(f"'{t}'" for t in skill["tools"])
    desc_esc = skill["desc"].replace("'", "\\\\'")
    return f"""export default {{
  id: 'hephaestus:{name}',
  displayName: 'Hephaestus: {skill["display"]}',
  model: 'anthropic/claude-sonnet-4',
  toolNames: [{tools_str}],
  instructionsPrompt:
    'You are executing the Hephaestus skill: {name}.\\n\\n' +
    '{desc_esc}\\n\\n' +
    '## Instructions\\n\\n' +
    '1. Read the skill definition file at skills/{name}/SKILL.md\\n' +
    '2. Parse the user\\'s request as the skill arguments\\n' +
    '3. Follow the instructions in the SKILL.md file precisely\\n' +
    '4. Use the tools available to you to complete the task\\n' +
    '5. Report results back to the user\\n\\n' +
    '## Context\\n\\n' +
    'This skill is part of the ProjectHephaestus plugin for the HomericIntelligence ecosystem.\\n' +
    'The project root contains all skill definitions under skills/.\\n' +
    'Project conventions are documented in CLAUDE.md.',
}}
"""
def main():
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    for skill in SKILLS:
        filepath = AGENTS_DIR / f"hephaestus-{skill['name']}.ts"
        filepath.write_text(generate_agent(skill))
        print(f"Created: {filepath}")
    print(f"\nGenerated {len(SKILLS)} agent definitions in {AGENTS_DIR}/")
if __name__ == "__main__":
    main()
