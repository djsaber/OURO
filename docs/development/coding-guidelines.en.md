# OURO Development Principles

> Document type: development standard
> Scope: current source, tests, documentation, and contribution workflow

The purpose of these guidelines is not to reduce the amount of code an Agent writes, but to ensure that what is written resembles a designed, maintainable, and evolvable system.

---

## 1. Goals and Definition of Done

- Your objective is not merely to make a feature work, but to integrate new behavior into a system that can evolve over time.
- All feature implementations, architectural designs, and system improvements must follow first principles. Identify the problem's essential nature, real constraints, key invariants, and necessary causal relationships before choosing an implementation. Do not let surface-level implementations, existing conventions, or local patches replace reasoning about the underlying problem.
- "It runs" is not a valid completion criterion. Done means: correct behavior, clear boundaries, reasonable dependencies, visible failures, trustworthy tests, and future modifiability.
- Always be wary of the outcome where all tests pass yet the system becomes harder to change. A decline in maintainability is itself a failure.
- Before finishing a task, perform at least one holistic review: Does this feature belong here naturally? Will adjacent features be easy to add later? Will errors surface clearly? Do the tests actually protect the intended behavior?

## 2. Understanding the Project and Layering Judgment

- Before modifying code, understand the project structure, module responsibilities, dependency directions, call chains, data flow, and existing conventions. Do not pile implementation onto a system whose overall shape you do not yet grasp.
- Before each implementation, determine which layer the requirement belongs to: domain logic, application services, infrastructure, interface adapters, configuration, tests, or tooling.
- Code must reside in the correct layer. APIs, CLIs, task queues, and scheduled jobs are entry points, not the business itself.
- Entry points are responsible for parsing input, invoking application services, and formatting output. They must not host core business rules.
- Do not place business logic into entry files, routing layers, CLIs, task schedulers, ORM models, database access layers, test fixtures, or utility functions.
- When architectural intent is unclear, do not guess and extend. First infer the design from existing code, README, tests, naming, and call relationships; if still uncertain, state your assumptions and trade-offs explicitly.

## 3. Module Responsibilities and Dependency Direction

- Every module must have a clear responsibility. It should be possible to describe what a module does—and what it does not do—in a single sentence.
- A module should ideally have only one primary reason to change. Do not mix code that changes due to business rules with code that changes due to technical implementation.
- Maintain clear and stable dependency directions. High-level business logic must not depend on low-level implementation details; core logic must not be bound to databases, HTTP, filesystems, CLIs, frameworks, or third-party SDKs.
- Prefer dependencies from outer layers toward inner layers. Interfaces call applications, applications orchestrate domain logic, and infrastructure implements external dependencies.
- Do not let "ease of import" dictate architecture. Import paths should reflect design relationships, not improvised connections.
- When cyclic dependencies appear, do not patch them with local imports. Cycles usually indicate a problem with responsibility partitioning or dependency direction.
- Do not hide dependencies behind global state. Configuration, clients, database sessions, time, randomness, and external services should be passed explicitly or managed centrally.

## 4. Interface Contracts and Data Structures

- Design public interfaces with care. Function signatures, return values, exceptions, types, and side effects all form part of the contract.
- Keep public interfaces stable. When modifying widely used functions, classes, schemas, events, or configuration items, review all callers and compatibility implications.
- Do not force callers to infer implicit rules. Input constraints, defaults, error conditions, state transitions, and edge cases must be expressed explicitly.
- Types are design tools. In Python projects, prefer type annotations to express inputs, outputs, optionality, domain objects, and error boundaries.
- Data structures should communicate intent. Prefer dataclasses, TypedDict, Pydantic models, Enum, Protocol, or domain objects over letting `dict[str, Any]` and raw strings flow throughout the system.
- A class should not be merely a container for functions. Use classes only when they encapsulate state, strategy, lifecycle, protocol, or a clear concept.
- Functions should be small and complete. They may do one complex thing, but they must not simultaneously assume responsibility for validation, querying, transformation, business decisions, persistence, and response formatting.

## 5. External Dependencies, Configuration, and Resource Boundaries

- Establish boundaries around external dependencies. Databases, caches, message queues, filesystems, third-party APIs, and model invocations should all be encapsulated within the infrastructure layer.
- Third-party libraries must be isolated behind adapter layers. Do not scatter the objects, exception types, and call details of any SDK across the project.
- Before adding a dependency, judge whether it is truly necessary. Dependencies bring versioning, security, startup time, learning cost, and abstraction leakage.
- Configuration must be centralized, strongly typed, validated, and overridable. Do not read environment variables scattered throughout business code.
- Non-deterministic sources such as time, randomness, UUIDs, current user, current environment, network, and filesystem should enter core logic through replaceable boundaries to facilitate testing and reproducibility.
- Resources must be closable. Files, connections, clients, thread pools, temporary directories, and locks should all have clear lifecycles.

## 6. Error Handling and Observability

- Errors are part of system behavior, not incidental byproducts. Every external call, parsing step, state change, permission check, and data transformation must consider its failure path.
- Do not swallow exceptions silently. Every catch must have a clear purpose: translating error types, adding context, retrying, rolling back, degrading gracefully, logging before re-raising, or returning an explicit result.
- Error messages should help locate problems. Exceptions and logs should include essential context, but must not leak keys, tokens, passwords, private data, or complete sensitive payloads.
- Do not substitute logging for error handling. Logging an error while the system continues to produce corrupt state is more dangerous than failing outright.
- Critical flows must be observable. Important operations, external calls, failure causes, retries, degradations, and state changes should include appropriate logging, metrics, or tracing points.

## 7. Testing Strategy

- Tests are not only proof that code runs; they are proof that failures can be detected.
- Important features should cover the happy path, invalid input, boundary conditions, dependency failures, permission failures, empty data, repeated execution, and state conflicts.
- Tests should verify behavior and contracts, not freeze irrelevant implementation details. Do not copy implementation details into tests, causing them to fail and succeed in lockstep with the code.
- For bug fixes, first write a test that reproduces the bug, then fix it. After the fix, the test should prove the bug cannot return.
- For architectural changes, supplement with integration-level or boundary tests. Unit tests alone are insufficient to prove that modules collaborate correctly.
- Use mocks with restraint. Mock external systems, slow operations, and non-deterministic boundaries; mocking your own core logic usually masks design problems.
- Test names should express the scenario and expected outcome. Reading a test should reveal system behavior, not merely a sequence of function calls.

## 8. Data, Transactions, Idempotency, and Compatibility

- Data persistence must have clear boundaries. Do not let ORM details pollute business rules, nor let database schema dictate the entire domain model.
- Database operations must have clear transactional boundaries. Do not allow half a business process to succeed while the other half fails, leaving the system in an inexplicable state.
- Important state changes must consider idempotency. Task retries, repeated API requests, and duplicate message consumption should not easily cause duplicate writes or dirty state.
- The current implementation does not provide compatibility or migration code for historical data, APIs, or configuration. Reject unsupported formats explicitly; users can use a fresh directory or an explicit data-maintenance procedure. Never clear user data automatically.
- Scripts and one-off tasks must be held to production-code standards. They can destroy data too.

## 9. Security, Concurrency, and Performance

- Security is not a final step. Input validation, permission checks, path handling, deserialization, command execution, SQL, template rendering, and secret management all require active scrutiny.
- Do not hard-code secrets, do not log sensitive information, do not trust external input, do not concatenate SQL, and do not bypass permission checks.
- Concurrent and asynchronous code must make explicit the handling of shared state, locks, timeouts, cancellation, retries, transactions, idempotency, and resource release.
- Performance optimization must be evidence-based. Do not over-engineer prematurely, but avoid obvious N+1 queries, repeated I/O, unbounded caches, and full-load retrieval.

## 10. Naming, Abstraction, and File Organization

- Names should reveal design intent. Class, function, and module names should tell the reader what something is and why it exists.
- Avoid vague names such as `manager`, `helper`, `utils`, or `processor`, unless they already carry a well-defined meaning within the project.
- Utility functions must be disciplined. `utils` is often an early sign of architectural rot; if a utility function carries business concepts, move it to the corresponding domain module.
- Do not over-abstract. Introduce new abstractions only when duplication is real, the direction of change is clear, and the abstraction reduces cognitive load.
- Likewise, do not leave logic as an amorphous mass. Long functions, large classes, and modules spanning multiple concepts should be split into named, bounded, and testable units.
- File organization should serve comprehension. Directory structure should let a new reader guess where code lives, rather than forcing them to rely solely on full-text search.
- Do not let a single file keep growing. File bloat usually indicates diffusion of responsibility; split by concept, workflow, or boundary.

## 11. Refactoring, Evolution, and Delivery

- When modifying existing code, respect the project's existing patterns. Do not introduce a radically different architecture unless the existing pattern is clearly harmful.
- Use the smallest necessary improvement that addresses the root cause when implementing features, improving the system, or fixing problems. Do not expand scope or add unnecessary layers, abstractions, states, or compatibility paths for hypothetical future needs. Every increase in complexity must be justified by the current problem and a verifiable benefit.
- Do not refactor unrelated code opportunistically. Refactoring must serve the current goal, have a clear scope, and preserve verifiable behavior.
- Refactoring must keep behavior unchanged. Separate functional changes from structural refactoring as much as possible, so that the source of any problem can be isolated.
- When the current architecture cannot support a new feature, first identify the structural problem, then perform the minimal necessary cleanup. Do not force new complexity into old crevices.
- Large changes should be staged. First establish boundaries and test protection, then migrate the implementation, and finally clean up old code.
- After each change, check whether it introduces unnecessary coupling, duplication, implicit dependencies, global state, or untested areas.
- Every commit or delivery should explain: what changed, why it was designed this way, which modules are affected, how to verify it, and what risks remain.

## 12. Documentation and Code Review

- Documentation should explain "why," not merely repeat "what." Complex designs, boundary choices, non-obvious constraints, and trade-offs deserve a written record.
- When updating documentation, describe the current design, implementation, and effective contracts directly. Do not include change notes, before-and-after comparisons, revision annotations, or historical evolution. Keep pending work and extension conditions in planning documents.
- Comments should serve the understanding of complexity. Do not comment on the obvious, and do not use comments to compensate for poor naming.
- In code review, prioritize systemic issues over local syntax: dependency direction, boundary leakage, error paths, test trustworthiness, and long-term maintainability.
- If a requirement pushes the project architecture in a worse direction, explicitly surface the risk and propose a more stable implementation path.

## 13. Code Formatting and Style Presentation

- Clear and consistent formatting improves the reader's experience. Maintain uniform indentation, blank lines, line breaks, and alignment so that code is visually scannable and well-layered.
- Complete and precise comments at key points improve comprehensibility. Leave concise annotations at complex algorithms, non-obvious decisions, boundary assumptions, and external contracts to help readers quickly establish context.
- Align code style with top-tier open-source projects, striving for presentation quality that withstands public scrutiny. Follow widely accepted community formatting standards such as PEP 8, Black, and Ruff.

---

## One-Sentence Guiding Principle

Writing code is not about stuffing features into a project; it is about integrating new behavior into an existing system. Running correctly is only the baseline—being understandable, modifiable, and error-detectable is what makes it done well.
