# Release Process

OURO is currently pre-1.0. Supported interfaces and persistence formats are defined in the current specs.
Unsupported stored formats fail explicitly; the development package does not provide historical
format adapters or migration commands.

Before the first public distribution, maintainers must choose and add a project license. Do not
publish the package or infer a license from dependencies before that decision is recorded.

For a release:

1. Verify the current capability summary in `CHANGELOG.md` and the affected specs.
2. Update the version in `pyproject.toml` and regenerate `uv.lock`.
3. Run the full commands from `CONTRIBUTING.md`, including the coverage floor.
4. Build with `uv build` and inspect both wheel and source archive for secrets and unintended files.
5. Test installation and `ouro --help` from a clean environment.
6. Commit the release metadata, create an annotated `vX.Y.Z` tag, and publish release notes from
   the capability summary and current specs. Keep repository documentation focused on current behavior.

Package-index publication and signing credentials are intentionally not automated until the
repository owner selects the destination, trusted-publishing policy, and license.
