# Changelog

This project records user-visible and framework-contract changes in this file.

## Unreleased

### Added

- Typed application configuration facade shared by CLI hosts.
- Provider retry policy with observable retry events and structured-output capability checks.
- Hard-deny tool permissions, workspace path policy, and bounded tool output.
- Request-local text and image resource projection from canonical Session resource references.
- Cursor-based incremental Session message and event queries.
- Cross-platform CI, branch coverage enforcement, and security/contribution guidance.

### Changed

- Configuration, Provider, Skills, and runtime composition CLI paths now use application services
  instead of infrastructure managers.
- Resource content remains immutable and content-addressed; model materialization no longer
  requires embedding large payloads in canonical message journals.

## 0.1.0

- Initial local-first agent runtime, Session/Run persistence, tool/HITL loop, Provider adapters,
  MCP client, Skills management, resource storage, compaction, and runtime supervision.
