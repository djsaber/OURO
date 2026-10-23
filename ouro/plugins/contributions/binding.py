"""Bind prepared contributions to a plugin generation."""

from __future__ import annotations

from dataclasses import replace

from ouro.plugins.contributions.models import PreparedContributionSet
from ouro.tools import Tool


def bind_execution_contract(
    prepared: PreparedContributionSet,
    execution_contract_hash: str,
) -> PreparedContributionSet:
    """Attach one stable generation contract to every durable contribution surface."""

    if len(execution_contract_hash) != 64:
        raise ValueError("execution contract hash must be a SHA-256 digest")
    plugin_id = prepared.owner.plugin_id
    tools = tuple(
        Tool(
            definition=tool.definition.model_copy(
                update={
                    "metadata": {
                        **tool.definition.metadata,
                        "execution_contract_hash": execution_contract_hash,
                    }
                }
            ),
            handler=tool.handler,
            resume_handler=tool.resume_handler,
        )
        for tool in prepared.tools
    )
    commands = tuple(
        replace(command, execution_contract_hash=execution_contract_hash)
        for command in prepared.commands
    )
    continuation_policies = tuple(
        replace(
            policy,
            execution_contract_hash=execution_contract_hash,
            contract_hash=prepared.contract_hashes[
                f"{plugin_id}/continuation_policies/{policy.policy_id}"
            ],
        )
        for policy in prepared.continuation_policies
    )
    session_controllers = tuple(
        replace(
            controller,
            contract_hash=prepared.contract_hashes[
                f"{plugin_id}/session_controllers/{controller.controller_id}"
            ],
            execution_contract_hash=execution_contract_hash,
        )
        for controller in prepared.session_controllers
    )
    skills = tuple(
        skill.model_copy(
            update={
                "extensions": {
                    **skill.extensions,
                    "ouro": {
                        **dict(skill.extensions.get("ouro", {})),
                        "execution_contract_hash": execution_contract_hash,
                    },
                }
            }
        )
        for skill in prepared.skills
    )
    return replace(
        prepared,
        tools=tools,
        commands=commands,
        continuation_policies=continuation_policies,
        run_drivers=tuple(
            replace(
                driver,
                execution_contract_hash=execution_contract_hash,
                contract_hash=prepared.contract_hashes[driver.canonical_id],
            )
            for driver in prepared.run_drivers
        ),
        session_controllers=session_controllers,
        skills=skills,
    )
