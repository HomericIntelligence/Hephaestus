export default {
  id: 'hephaestus:tidy',
  displayName: 'Hephaestus: Tidy',
  model: 'anthropic/claude-sonnet-4',
  toolNames: ['read_files', 'run_terminal_command', 'end_turn'],
  instructionsPrompt:
    'You are executing the Hephaestus skill: tidy.\n\n' +
    'Rebases all local branches with swarm conflict resolution.\n\n' +
    '## Instructions\n\n' +
    '1. Read the skill definition file at skills/tidy/SKILL.md\n' +
    '2. Parse the user\'s request as the skill arguments\n' +
    '3. Follow the instructions in the SKILL.md file precisely\n' +
    '4. Use the tools available to you to complete the task\n' +
    '5. Report results back to the user\n\n' +
    '## Context\n\n' +
    'This skill is part of the ProjectHephaestus plugin for the HomericIntelligence ecosystem.\n' +
    'The project root contains all skill definitions under skills/.\n' +
    'Project conventions are documented in CLAUDE.md.',
}
