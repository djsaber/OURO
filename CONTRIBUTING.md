# Contributing to OURO

OURO targets Python 3.14 or newer and uses `uv` for its locked development environment.

Use the fast architecture/core gate while iterating:

```bash
uv run --frozen pytest -q tests/architecture tests/core
```

Before handoff, run the complete local quality gate:

```bash
uv sync --frozen --all-groups
uv run --frozen pytest --cov=ouro --cov-report=term-missing
uv run --frozen ruff check ouro tests
uv run --frozen ruff format --check ouro tests
uv run --frozen mypy ouro
uv run --frozen python -m compileall -q ouro
uv build
```

Run platform-specific checks on their actual target systems and report skipped or unverified
platforms separately; a passing local gate is not cross-platform evidence.

Implementation and review must follow the
[Chinese coding guidelines](docs/development/coding-guidelines.zh-CN.md) or the
[English coding guidelines](docs/development/coding-guidelines.en.md). Keep domain contracts in `core`,
orchestration in `application`, adapters in infrastructure packages, and presentation behavior in
hosts such as `cli`. Do not make a Host depend directly on persistence files or configuration
managers when an application boundary exists.

Documentation follows the [documentation standard](docs/development/documentation.md). Keep user
guides, formal specifications (`docs/specs/`), future plans, and completed reviews in their respective locations;
update relative links and command examples when moving or changing them.

Changes should include focused success and failure tests. Concurrency, cancellation, recovery,
credential redaction, path handling, and durable-state changes require explicit regression
coverage. Keep generated caches, local OURO homes, credentials, and build artifacts out of commits.

Before requesting review:

- explain the behavior and compatibility impact;
- keep specs and the capability summary self-contained descriptions of current contracts;
- run the complete quality gate above;
- identify supported data formats, security assumptions, and intentionally deferred work;
- avoid bundling unrelated refactors into the same change.

The repository does not yet declare a distribution license. Contributors and maintainers must
resolve that legal choice before publishing packages or accepting contributions under an assumed
license.
