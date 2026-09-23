# Contributing to OURO

OURO targets Python 3.14 or newer and uses `uv` for its locked development environment.

Use the fast architecture/core gate while iterating:

```bash
uv run --frozen pytest -q tests/architecture tests/core
```

Before handoff, run the complete cross-platform-equivalent quality gate:

```bash
uv sync --frozen --all-groups
uv run pytest --cov=ouro --cov-report=term-missing
uv run ruff check ouro tests
uv run ruff format --check ouro tests
uv run mypy ouro
uv run python -m compileall -q ouro
uv build
```

Implementation and review must follow the
[Chinese coding guidelines](docs/OURO_编码准则_ZH_2026-09-02.md) or the
[English coding guidelines](docs/OURO_编码准则_EN_2026-09-02.md). Keep domain contracts in `core`,
orchestration in `application`, adapters in infrastructure packages, and presentation behavior in
hosts such as `cli`. Do not make a Host depend directly on persistence files or configuration
managers when an application boundary exists.

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
