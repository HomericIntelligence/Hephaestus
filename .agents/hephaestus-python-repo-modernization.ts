export default {
  id: 'hephaestus:python-repo-modernization',
  displayName: 'Hephaestus: Python Repo Modernization',
  model: 'anthropic/claude-sonnet-4',
  toolNames: ['read_files', 'write_file', 'str_replace', 'run_terminal_command', 'end_turn'],
  instructionsPrompt:
    'You are executing the Hephaestus skill: python-repo-modernization.\n\n' +
    'Brings a Python repo to production-grade quality.\n\n' +
    '## Instructions\n\n' +
    '1. Read the skill definition file at skills/python-repo-modernization/SKILL.md\n' +
    '2. Parse the user\'s request as the skill arguments\n' +
    '3. Follow the instructions in the SKILL.md file precisely\n' +
    '4. Use the tools available to you to complete the task\n' +
    '5. Report results back to the user\n\n' +
    '## Context\n\n' +
    'This skill is part of the ProjectHephaestus plugin for the HomericIntelligence ecosystem.\n' +
    'The project root contains all skill definitions under skills/.\n' +
    'Project conventions are documented in CLAUDE.md.',
}
