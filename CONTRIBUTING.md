# Contributing to OURO

OURO targets Python 3.14 or newer and uses `uv` for its locked development environment.

Use the fast architecture/core gate while iterating:

```bash
uv run --frozen pytest -q tests/architecture tests/core
```

Before handoff, run the complete local quality gate:

```bash
uv sync --frozen --all-groups --extra themes
uv run --frozen --extra themes pytest --cov=ouro --cov-report=term-missing
uv run --frozen --extra themes ruff check ouro tests dev/check_distribution.py
uv run --frozen --extra themes ruff format --check ouro tests dev/check_distribution.py
uv run --frozen --extra themes mypy ouro
uv run --frozen --extra themes python -m compileall -q ouro
uv build
```

The full suite includes optional theme renderers. `--all-groups` selects development groups;
`--extra themes` selects the user-facing theme dependencies. Linux source builds may need the
compiler settings in the [theme installation guide](docs/guides/running.md#欢迎页主题).

Also verify the base installation in a separate environment without optional dependencies:

```bash
uv run --isolated --frozen pytest -q tests/architecture tests/core tests/tui/test_optional_welcome_styles.py
```

After building, run the installed-distribution smoke check twice in isolated environments, using
the wheel path in place of `<wheel>` (quote the requirement in PowerShell, bash or zsh):

```bash
uv run --isolated --no-project --with "<wheel>" python -I dev/check_distribution.py
uv run --isolated --no-project --with "<wheel>[themes]" python -I dev/check_distribution.py --themes
```

These checks verify dependency metadata, CLI entry points, actual installed module paths, TUI
startup, available theme choices and preference preservation without relying on the source checkout.

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

OURO is distributed under the [MIT License](LICENSE). Contributions to this repository are
provided under the same license. Preserve applicable copyright and license notices.
