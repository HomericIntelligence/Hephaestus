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
