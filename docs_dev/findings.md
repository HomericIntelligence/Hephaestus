# Findings: Hephaestus Plugin for Codebuff

## Codebuff Agent System
- Agents are TypeScript files in `.agents/` directory
- Models use OpenRouter format (e.g., `anthropic/claude-sonnet-4`)
- Tools: `read_files`, `write_file`, `str_replace`, `run_terminal_command`, `spawn_agents`, `end_turn`

## Tool Mapping (Claude Code → Codebuff)
| Claude Code | Codebuff |
|-------------|----------|
| Read | read_files |
| Write | write_file |
| Edit | str_replace |
| Bash | run_terminal_command |
| Agent | spawn_agents |
