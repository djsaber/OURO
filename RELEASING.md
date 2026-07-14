# Release Process

OURO is currently pre-1.0. Patch releases should remain backward compatible within the documented
`0.1.x` contracts; incompatible persistence or public API changes require an explicit migration
plan and a minor-version change.

Before the first public distribution, maintainers must choose and add a project license. Do not
publish the package or infer a license from dependencies before that decision is recorded.

For a release:

1. Move relevant `Unreleased` entries into a dated version section in `CHANGELOG.md`.
2. Update the version in `pyproject.toml` and regenerate `uv.lock`.
3. Run the full commands from `CONTRIBUTING.md`, including the coverage floor.
4. Build with `uv build` and inspect both wheel and source archive for secrets and unintended files.
5. Test installation and `ouro --help` from a clean environment.
6. Commit the release metadata, create an annotated `vX.Y.Z` tag, and publish release notes from
   the changelog.

Package-index publication and signing credentials are intentionally not automated until the
repository owner selects the destination, trusted-publishing policy, and license.
