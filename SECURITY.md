# Security Policy

## Supported code

Security fixes target the current default branch and the latest `0.1.x` release while the
project remains pre-1.0. Older snapshots are not maintained as separate security branches.

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
- Workspace-only mode constrains built-in file paths and detected-shell working directories. It is not an
  operating-system sandbox: an approved shell command can still access resources allowed to the
  OURO process.
- Prefer environment-backed credential references. Inline credentials are stored as local plain
  text even though public DTO and CLI output redact them.
- MCP servers, plugins, Skills, imported resources, and model-produced tool arguments are
  untrusted input. Review their provenance and permissions before enabling them.

Reports involving credential disclosure, path escape, command execution without authorization,
resource integrity bypass, or cross-Session state corruption should be treated as high priority.
