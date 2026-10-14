# Release Process

Release versions are owned by `pyproject.toml`, `ouro.__version__`, the lockfile, and release tags.
Completion of a feature milestone does not change those metadata automatically. Supported interfaces
and persistence formats are defined in the [current specs](docs/specs/README.md).
Runtime readers reject unsupported stored formats. The independent `ouro-upgrade` command and TUI
startup recovery convert explicitly supported historical core data; see the
[data upgrade specification](docs/specs/data-upgrade.md) for support and recovery boundaries.

OURO uses the [MIT License](LICENSE). Release metadata must declare MIT, and both wheel and source
archives must include the license text. Dependencies retain their own licenses.

For a release:

1. Update [CHANGELOG.md](CHANGELOG.md) for every version iteration. Keep entries in reverse
   chronological order with a version, release date and concise user-facing update items, including
   material compatibility changes. Keep validation results in commit messages or review records;
   omit implementation details, redundant explanations and a commit-by-commit history. Preserve
   earlier entries and label historical development versions without inventing release dates.
   Verify the [capability overview](docs/overview.md) and affected specs against the release candidate.
2. Update `pyproject.toml` and `ouro.__version__`, align plugin host-version ranges, and regenerate `uv.lock`.
3. Run the full commands from `CONTRIBUTING.md`, including the coverage floor.
4. Build with `uv build` and inspect both wheel and source archive for secrets and unintended files.
5. Test both the base wheel and its `themes` extra from separate clean environments using
   `dev/check_distribution.py` as described in `CONTRIBUTING.md`. Verify that `arrayfunc` is absent
   from the base installation and available to the theme installation. Run platform checks on the
   actual target systems; preserve documented compiler requirements for optional source builds.
6. Commit the release metadata, create an annotated `vX.Y.Z` tag, and publish release notes from
   the reviewed changelog entry. Keep guides and specs focused on current behavior; release history
   belongs in the changelog.

The public package index is PyPI, under the distribution name `ouro-agent`. Local publication uses
an API token supplied through `UV_PUBLISH_TOKEN`; do not place credentials in source files, command
arguments or release notes. CI publication and signing are not automated; configure Trusted Publishing
before adding unattended CI uploads.

Before uploading, validate the user-facing README with `twine check --strict`, verify that its examples
work from an installed distribution, and use absolute links that also work on PyPI. Keep screenshots
in `docs/assets/`, capture real application state without private data, and link the published image
through an immutable release tag. Verify that all public links resolve before uploading. Keep the exact
version's wheel and source archive aligned with its release commit and tag. Pass those two filenames
explicitly to `uv publish` so older files in `dist/` are not uploaded. After uploading, verify PyPI's
version metadata, description and file hashes, and check installation from the published index.

During `0.x` development, use a new minor version for a capability milestone or incompatible public
contract change, and a patch version for compatible fixes. Plugin API, worker protocol, package and
stored-data versions remain separate contracts; changing the product version does not migrate them.
Keep plugin host-version ranges in builtins, scaffolds, examples and fixtures aligned with the release.
Runtime version reporting and installation provenance use `ouro.__version__`.

A release commit records the reviewed changes; an annotated `vX.Y.Z` tag identifies that exact
snapshot. A separate release branch is only needed when maintaining parallel release lines. Local
commits and tags do not push to a remote or publish a package.
