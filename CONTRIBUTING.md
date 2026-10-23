# Contributing to OURO

OURO targets Python 3.14 or newer and uses `uv` for its locked development environment.

Use the fast architecture/core gate while iterating:

```bash
uv run --frozen pytest -q tests/architecture tests/core
```

Before handoff, run the complete local quality gate:

```bash
uv sync --frozen --all-groups --extra themes
uv run --frozen --extra themes pytest -n 4 --dist worksteal --durations=20 --cov=ouro --cov-report=term-missing
uv run --frozen --extra themes ruff check ouro packages tests dev/check_distribution.py
uv run --frozen --extra themes ruff format --check ouro packages tests dev/check_distribution.py
uv run --frozen --extra themes mypy --no-incremental --platform win32 ouro packages/tui-themes/src/ouro_tui_themes
uv run --frozen --extra themes python -m compileall -q ouro packages/tui-themes/src
uv build --all-packages
```

The full suite includes optional theme renderers. `--all-groups` selects development groups;
`--extra themes` installs the complete `ouro-tui-themes` workspace package, including the shared
dot-matrix engine, official styles and NumPy. Core and extension distributions have independent
versions: the core's `themes` extra selects an extension release, and the extension declares its
supported core range. Build both workspace packages and publish any required, unpublished release.
See the [theme installation guide](docs/guides/running.md#欢迎页主题).

The single CI static-check job runs on Windows with theme dependencies installed. The explicit
`win32` mypy target covers the native Windows adapters; disabling incremental reads ensures the
current dependency environment is checked. Linux and macOS run the full runtime and installation gates.

Also verify the base installation in a separate environment without optional dependencies:

```bash
uv run --isolated --frozen pytest -q tests/architecture tests/core tests/tui/test_optional_welcome_styles.py tests/tui/test_welcome_package_recovery.py
```

After building, run the installed-distribution smoke check twice in isolated environments, using
the core wheel path in place of `<wheel>`. Both built wheels must be present in `dist`
(quote the requirement in PowerShell, bash or zsh):

```bash
uv run --isolated --no-project --with "<wheel>" python -I dev/check_distribution.py
uv run --isolated --no-project --find-links dist --with "<wheel>[themes]" python -I dev/check_distribution.py --themes
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

Retain tests that detect a concrete behavior, contract, safety or resource-cost regression.
When removing a test, identify the obsolete requirement or the remaining tests that protect its
useful assertions. Similar cases across storage backends, protocols and process boundaries are
not duplicates merely because their setup looks alike. Prefer controlled clocks and event
handshakes to short wall-clock deadlines; use real timeouts only as hang guards or when testing
the timeout mechanism itself. Exclude inapplicable parameter combinations at collection time.
Keep machine-speed measurements in `dev/benchmarks`, and use behavior checks instead of source
text scans or snapshots of incidental artwork. Platform-specific regressions remain in the suite
with explicit prerequisite checks.

Use component tests for widget and menu behavior, and full applications for Host integration.
Visual appearance is checked manually; see the [test strategy](docs/development/testing.md)
for test selection, asynchronous synchronization, profiling and visual review.

Before requesting review:

- explain the behavior and compatibility impact;
- keep specs and the capability summary self-contained descriptions of current contracts;
- run the complete quality gate above;
- identify supported data formats, security assumptions, and intentionally deferred work;
- avoid bundling unrelated refactors into the same change.

OURO is distributed under the [MIT License](LICENSE). Contributions to this repository are
provided under the same license. Preserve applicable copyright and license notices.
