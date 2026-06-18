export default {
  id: 'hephaestus:repo-analyze-full',
  displayName: 'Hephaestus: Repo Analyze Full',
  model: 'anthropic/claude-sonnet-4',
  toolNames: ['read_files', 'run_terminal_command', 'spawn_agents', 'end_turn'],
  instructionsPrompt:
    'You are executing the Hephaestus skill: repo-analyze-full.\n\n' +
    'Full-coverage audit via swarm agents.\n\n' +
    '## Instructions\n\n' +
    '1. Read the skill definition file at skills/repo-analyze-full/SKILL.md\n' +
    '2. Parse the user\'s request as the skill arguments\n' +
    '3. Follow the instructions in the SKILL.md file precisely\n' +
    '4. Use the tools available to you to complete the task\n' +
    '5. Report results back to the user\n\n' +
    '## Context\n\n' +
    'This skill is part of the ProjectHephaestus plugin for the HomericIntelligence ecosystem.\n' +
    'The project root contains all skill definitions under skills/.\n' +
    'Project conventions are documented in CLAUDE.md.',
}
