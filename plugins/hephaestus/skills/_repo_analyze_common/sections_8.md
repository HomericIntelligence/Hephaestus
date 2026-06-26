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
