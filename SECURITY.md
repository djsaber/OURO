# Security Policy

## Supported code

Security fixes target the current default branch. Any maintained release branches must be
explicitly identified in their release notes; this policy does not infer support from the package
version or a completed feature milestone. Older snapshots are not maintained as separate security branches.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Prefer the repository's private
GitHub Security Advisory flow. If that facility is unavailable, contact a maintainer through a
private channel and include the affected revision, impact, reproduction steps, and any proposed
mitigation. Avoid including real credentials or private user data in the report.

Maintainers should acknowledge the report, reproduce and classify it, prepare a regression test
and fix, and coordinate disclosure after supported users have an upgrade path. Exact response
times depend on maintainer availability and are not guaranteed.

## Security boundaries

- `deny` is the hard tool-disable policy; `approval` is a human authorization boundary.
- The Session workspace provides task context, relative-path resolution, and a default working
  directory. OURO does not provide filesystem or network sandboxing; resource access follows
  host-system and external-service permissions.
- Prefer environment-backed credential references. Inline credentials are stored as local plain
  text even though public DTO and CLI output redact them.
- MCP servers, plugins, Skills, imported resources, and model-produced tool arguments are
  untrusted input. Review their provenance and permissions before enabling them.
- Enabled code plugins run as trusted code. Host-service permissions govern those service calls,
  not direct Python or operating-system access.

Reports involving credential disclosure, package extraction path traversal, command execution without authorization,
resource integrity bypass, or cross-Session state corruption should be treated as high priority.
