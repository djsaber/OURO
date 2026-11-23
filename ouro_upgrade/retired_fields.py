"""Retire redundant fields only at explicitly owned persistence paths."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from ouro_upgrade.agents import upgrade_definition, upgrade_spawn_policy
from ouro_upgrade.errors import require

_CONTROL_FIELDS = ("phase", "revision", "updated_at", "error")


def _controls(prefix: str) -> tuple[str, ...]:
    return tuple(f"{prefix}.activations.*.{field}" for field in _CONTROL_FIELDS)


PATHS = {
    "agent_definition": ("can_spawn", "contract_hash"),
    "agent_catalog": (
        "spawn_policy.allowed_lifecycle_bindings",
        "contract_hash",
        "entries.*.effective_tools.*.execution_contract_hash",
    ),
    "input": ("item.skill_activations.*.content_digest", "item.driver.contract_hash"),
    "message": ("content.*.content_digest",),
    "context": ("provider_context.messages.*.content.*.content_digest",),
    "session": ("title_locked", *_controls("controls")),
    "harness": _controls("state.controls"),
    "run": (
        "metadata.agent_definition_contract_hash",
        "driver.contract_hash",
        "continuation.controllers.*.builtin_contract_hash",
        "continuation.controllers.*.contract_hash",
        "continuation.settlement_plan.contract_hash",
        *_controls("continuation.settlement_plan.harness.state.controls"),
        "continuation.settlement_plan.session_control_transitions.*.expected_activation_revision",
    ),
    "delegation": (
        "agent_id",
        "delivery_policy",
        "depth",
        "lifecycle_binding",
        "effective_tools.*.execution_contract_hash",
    ),
    "tool_state": (
        "prepared_binding.builtin_contract_hash",
        "agent_wait.state",
        "agent_wait.resolution.resolved_delegation_id",
        "agent_wait.resolution.resolved_child_run_id",
        *_controls("proposals.harness_change.controls"),
        *_controls("input_request.harness_change.controls"),
        "proposals.session_control_transitions.*.expected_activation_revision",
    ),
}


def members_at(value: Any, path: list[str]) -> Iterator[tuple[dict[str, Any], str]]:
    if value is None:
        return
    part, *rest = path
    if part == "*":
        for child in value:
            yield from members_at(child, rest)
    elif part in value:
        if rest:
            yield from members_at(value[part], rest)
        else:
            yield value, part


def members(kind: str, value: dict[str, Any]) -> Iterator[tuple[dict[str, Any], str]]:
    for path in PATHS.get(kind, ()):
        yield from members_at(value, path.split("."))


def sql_predicate(kind: str, *, column: str = "payload", prefix: str = "$") -> str:
    def exists(path: str, value: str, root: str, depth: int = 0) -> str:
        parent, wildcard, rest = path.partition(".*.")
        if not wildcard:
            return f"json_type({value}, '{root}.{path}') IS NOT NULL"
        alias = f"retired_{depth}"
        nested = exists(rest, f"{alias}.value", "$", depth + 1)
        return (
            f"EXISTS (SELECT 1 FROM json_each({value}, '{root}.{parent}') AS {alias} "
            f"WHERE {nested})"
        )

    return " OR ".join(exists(path, column, prefix) for path in PATHS.get(kind, ()))


def retire(kind: str, value: dict[str, Any]) -> None:
    if kind == "agent_definition":
        upgrade_definition(value)
        return
    if kind == "agent_catalog":
        upgrade_spawn_policy(value["spawn_policy"])
        value.pop("contract_hash", None)
        for entry in value["entries"]:
            for tool in entry["effective_tools"]:
                tool.pop("execution_contract_hash", None)
        return
    if kind == "delegation":
        expected = {
            "agent_id": value["definition"]["agent_id"],
            "lifecycle_binding": "bound",
            "depth": 1,
            "delivery_policy": "notify_only"
            if value.get("parent_tool_call_id") is None
            else ("resume_waiter" if value["execution_mode"] == "sync" else "next_boundary"),
        }
        for key, actual in expected.items():
            require(key not in value or value[key] == actual, kind, f"inconsistent {key}")
    if kind == "tool_state" and value.get("agent_wait"):
        wait = value["agent_wait"]
        resolution = wait.get("resolution")
        require(
            "state" not in wait or wait["state"] == ("resolved" if resolution else "pending"),
            kind,
            "inconsistent agent wait state",
        )
        if resolution:
            for key, target in (
                ("resolved_delegation_id", "delegation_id"),
                ("resolved_child_run_id", "child_run_id"),
            ):
                require(
                    resolution.get(key) in (None, wait[target]), kind, "foreign wait resolution"
                )
    for owner, field in list(members(kind, value)):
        if field == "phase":
            require(owner[field] == "active", kind, "unsupported control activation phase")
        owner.pop(field)
