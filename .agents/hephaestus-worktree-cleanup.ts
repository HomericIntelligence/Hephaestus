export default {
  id: 'hephaestus:worktree-cleanup',
  displayName: 'Hephaestus: Worktree Cleanup',
  model: 'anthropic/claude-sonnet-4',
  toolNames: ['read_files', 'run_terminal_command', 'end_turn'],
  instructionsPrompt:
    'You are executing the Hephaestus skill: worktree-cleanup.\n\n' +
    'Audits and prunes git worktrees.\n\n' +
    '## Instructions\n\n' +
    '1. Read the skill definition file at skills/worktree-cleanup/SKILL.md\n' +
    '2. Parse the user\'s request as the skill arguments\n' +
    '3. Follow the instructions in the SKILL.md file precisely\n' +
    '4. Use the tools available to you to complete the task\n' +
    '5. Report results back to the user\n\n' +
    '## Context\n\n' +
    'This skill is part of the ProjectHephaestus plugin for the HomericIntelligence ecosystem.\n' +
    'The project root contains all skill definitions under skills/.\n' +
    'Project conventions are documented in CLAUDE.md.',
}
