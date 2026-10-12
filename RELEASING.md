# Release Process

Release versions are owned by `pyproject.toml`, `ouro.__version__`, the lockfile, and release tags.
Completion of a feature milestone does not change those metadata automatically. Supported interfaces
and persistence formats are defined in the [current specs](docs/specs/README.md).
Unsupported stored formats fail explicitly; the current implementation does not provide historical
format adapters or migration commands.

OURO uses the [MIT License](LICENSE). Release metadata must declare MIT, and both wheel and source
archives must include the license text. Dependencies retain their own licenses.

For a release:

1. Update [CHANGELOG.md](CHANGELOG.md) for every version iteration. Keep entries in reverse
   chronological order with a version, release date and concise user-facing improvements. Summarize
   material compatibility impact, validation scope and known limitations; link to detailed records
   instead of including implementation details, test logs or a commit-by-commit history. Preserve
   earlier entries and label historical development versions without inventing release dates.
   Verify the [capability overview](docs/overview.md) and affected specs against the release candidate.
2. Update `pyproject.toml` and `ouro.__version__`, align plugin host-version ranges, and regenerate `uv.lock`.
3. Run the full commands from `CONTRIBUTING.md`, including the coverage floor.
4. Build with `uv build` and inspect both wheel and source archive for secrets and unintended files.
5. Test installation and `ouro --help` from a clean environment.
6. Commit the release metadata, create an annotated `vX.Y.Z` tag, and publish release notes from
   the reviewed changelog entry. Keep guides and specs focused on current behavior; release history
   belongs in the changelog.

Package-index publication and signing credentials are intentionally not automated until the
repository owner selects the destination and trusted-publishing policy.

During `0.x` development, use a new minor version for a capability milestone or incompatible public
contract change, and a patch version for compatible fixes. Plugin API, worker protocol, package and
stored-data versions remain separate contracts; changing the product version does not migrate them.
Keep plugin host-version ranges in builtins, scaffolds, examples and fixtures aligned with the release.
Runtime version reporting and installation provenance use `ouro.__version__`.

A release commit records the reviewed changes; an annotated `vX.Y.Z` tag identifies that exact
snapshot. A separate release branch is only needed when maintaining parallel release lines. Local
commits and tags do not push to a remote or publish a package.
