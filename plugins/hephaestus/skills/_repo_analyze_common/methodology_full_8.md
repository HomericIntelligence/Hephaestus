## Methodology

**Coverage:** Every file in the repository.

Step 1: Inventory all source files via `find` into a temporary file.

Step 2: Dispatch 8 agents in 2 waves of 4 (max 5 concurrent per the Myrmidon swarm constraint):

- Wave 1 agents: Sections 1–4
- Wave 2 agents: Sections 5–8

Each section agent receives the full file inventory and focuses deeply on files relevant to its section.

Step 3: Compile each agent's report into the final assessment. If a section agent did not return, re-dispatch it before finalizing the report.

Full coverage ensures no bugs are missed due to sampling limitations.
