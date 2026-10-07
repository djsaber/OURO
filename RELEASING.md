# Release Process

Release versions are owned by `pyproject.toml`, the lockfile, and release tags. Completion of a
feature milestone does not change those metadata automatically. Supported interfaces and persistence
formats are defined in the [current specs](docs/specs/README.md).
Unsupported stored formats fail explicitly; the current implementation does not provide historical
format adapters or migration commands.

Before the first public distribution, maintainers must choose and add a project license. Do not
publish the package or infer a license from dependencies before that decision is recorded.

For a release:

1. Prepare the release entry in [CHANGELOG.md](CHANGELOG.md), including behavior changes,
   compatibility impact, validation, and known limitations. Verify the [capability overview](docs/overview.md)
   and affected specs against the release candidate.
2. Update the version in `pyproject.toml` and regenerate `uv.lock`.
3. Run the full commands from `CONTRIBUTING.md`, including the coverage floor.
4. Build with `uv build` and inspect both wheel and source archive for secrets and unintended files.
5. Test installation and `ouro --help` from a clean environment.
6. Commit the release metadata, create an annotated `vX.Y.Z` tag, and publish release notes from
   the reviewed changelog entry. Keep guides and specs focused on current behavior; release history
   belongs in the changelog.

Package-index publication and signing credentials are intentionally not automated until the
repository owner selects the destination, trusted-publishing policy, and license.
