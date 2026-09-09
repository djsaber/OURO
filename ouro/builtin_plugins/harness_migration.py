"""Known builtin schema conversion, prepared without publishing any state or usage."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from ouro.builtin_plugins.goal.state import GoalRecord, SessionGoalState
from ouro.builtin_plugins.plan.state import SessionPlanState
from ouro.core.billing import ProviderUsageEntry
from ouro.core.errors import MigrationRequiredError
from ouro.core.harness import HarnessVersion
from ouro.core.session_commits import HarnessMigration
from ouro.core.state import SessionRecord
from ouro.plugins.session_controllers import PluginSessionControllerRegistry

_GOAL = "dev.ouro.goal"
_PLAN = "dev.ouro.plan"


@dataclass(frozen=True)
class BuiltinHarnessMigration:
    request: HarnessMigration | None
    usage_replacements: tuple[ProviderUsageEntry, ...] = ()


def prepare_builtin_harness_migration(
    session: SessionRecord,
    versions: tuple[HarnessVersion, ...],
    *,
    entries: tuple[ProviderUsageEntry, ...] | None,
    controllers: PluginSessionControllerRegistry,
) -> BuiltinHarnessMigration:
    """Convert reachable Plan 1 / Goal 3 values, retaining all original checkpoints."""

    replacements = {}
    accounted: dict[str, str] = {}
    run_goals: dict[str, str] = {}
    expected_tokens: dict[str, int] = {}
    unaccounted: set[str] = set()
    for version in versions:
        states = []
        controls = version.state.controls.model_copy(deep=True)
        changed = False
        for record in version.state.states:
            raw = record.payload
            if record.owner_id not in {_GOAL, _PLAN}:
                states.append(record)
                continue
            target_schema = 4 if record.owner_id == _GOAL else 2
            source_schema = 3 if record.owner_id == _GOAL else 1
            if record.state_schema_version == target_schema:
                states.append(record)
                continue
            if record.state_schema_version != source_schema or not isinstance(raw, dict):
                raise MigrationRequiredError(f"unsupported builtin state: {record.owner_id}")
            goal = None
            canonical = f"{record.owner_id}/session_controllers/mode"
            owned = [item for item in controls.activations if item.controller_id == canonical]
            if any(item.phase != "active" for item in owned):
                raise MigrationRequiredError("legacy controller lifecycle is not settled")
            if record.owner_id == _PLAN:
                phase = raw.get("phase", "inactive")
                if phase not in {"inactive", "active", "awaiting_approval"}:
                    raise MigrationRequiredError("unsupported legacy Plan phase")
                if (phase != "inactive") != bool(owned):
                    raise MigrationRequiredError(
                        "legacy Plan mode has no consistent native control"
                    )
                payload = SessionPlanState.from_json(raw).to_json()
            else:
                goal_raw = raw.get("goal")
                if goal_raw is not None:
                    if not isinstance(goal_raw, dict):
                        raise MigrationRequiredError("legacy Goal record must be an object")
                    goal_id = (
                        "goal_"
                        + uuid5(
                            NAMESPACE_URL,
                            f"ouro:{session.session_id}:goal:{goal_raw['created_at']}",
                        ).hex
                    )
                    goal = GoalRecord.from_json({**goal_raw, "goal_id": goal_id})
                    tokens = goal_raw.get("tokens_used")
                    if type(tokens) is not int or tokens < 0:
                        raise MigrationRequiredError("legacy Goal usage baseline is invalid")
                    expected_tokens[goal_id] = max(expected_tokens.get(goal_id, 0), tokens)
                    run_id = goal_raw.get("last_accounted_run_id")
                    if isinstance(run_id, str):
                        accounted[run_id] = goal_id
                    binding = raw.get("run_binding")
                    bound_run = binding.get("run_id") if isinstance(binding, dict) else None
                    if isinstance(bound_run, str):
                        unaccounted.add(bound_run)
                    for identity in (run_id, bound_run):
                        if (
                            isinstance(identity, str)
                            and run_goals.setdefault(identity, goal_id) != goal_id
                        ):
                            raise MigrationRequiredError(
                                "legacy Run usage belongs to multiple Goals"
                            )
                payload = SessionGoalState(goal=goal).to_json()
            for activation in owned:
                try:
                    controller = controllers.get(canonical)
                except KeyError as exc:
                    raise MigrationRequiredError(
                        f"builtin controller is unavailable: {canonical}"
                    ) from exc
                if (
                    controller.release_id is None
                    or controller.execution_contract_hash is None
                    or controller.generation_id is None
                ):
                    raise MigrationRequiredError(
                        f"builtin controller has no current contract: {canonical}"
                    )
                activation.release_id = controller.release_id
                activation.generation_id = controller.generation_id
                activation.execution_contract_hash = controller.execution_contract_hash
                activation.data = {}
                if record.owner_id == _GOAL:
                    if goal is None or goal.status == "complete":
                        raise MigrationRequiredError(
                            "legacy Goal state disagrees with its controller"
                        )
                    activation.usage_scope = goal.goal_id if goal.status == "active" else None
            states.append(
                record.model_copy(
                    update={
                        "state_schema_version": target_schema,
                        "payload": payload,
                    }
                )
            )
            changed = True
        if changed:
            state = version.state.model_copy(update={"controls": controls, "states": tuple(states)})
            identity = hashlib.sha256(
                f"{version.version_id}:{state.model_dump_json()}".encode()
            ).hexdigest()
            replacements[version.version_id] = HarnessVersion(
                session_id=session.session_id,
                version_id=f"harness_{identity}",
                state=state,
            )
    if not replacements:
        return BuiltinHarnessMigration(request=None)
    usage = _associate_usage(
        entries, session.session_id, accounted, expected_tokens, unaccounted - accounted.keys()
    )
    return BuiltinHarnessMigration(
        request=HarnessMigration(before=session, replacements=replacements),
        usage_replacements=usage,
    )


def _associate_usage(
    entries: tuple[ProviderUsageEntry, ...] | None,
    session_id: str,
    accounted: dict[str, str],
    expected: dict[str, int],
    unaccounted: set[str],
) -> tuple[ProviderUsageEntry, ...]:
    if not expected:
        return ()
    if entries is None:
        raise MigrationRequiredError("legacy Goal requires a verifiable native usage baseline")
    totals = dict.fromkeys(expected, 0)
    replacements = []
    for entry in entries:
        if entry.session_id != session_id:
            continue
        if entry.run_id in unaccounted:
            raise MigrationRequiredError("legacy Goal has unsettled Run usage")
        goal_id = accounted.get(entry.run_id)
        if goal_id is None:
            continue
        totals[goal_id] += entry.total_tokens
        scopes = entry.harness_scopes
        existing = scopes.get(_GOAL)
        if existing is not None:
            if existing != (goal_id,):
                raise MigrationRequiredError("legacy usage has a conflicting Goal attribution")
            continue
        scopes[_GOAL] = (goal_id,)
        replacements.append(
            entry.model_copy(
                update={
                    "revision": entry.revision + 1,
                    "metadata": {**entry.metadata, "harness_scopes": scopes},
                }
            )
        )
    if totals != expected:
        raise MigrationRequiredError(
            "legacy Goal counter cannot be proven from native usage; source state is retained"
        )
    return tuple(replacements)
