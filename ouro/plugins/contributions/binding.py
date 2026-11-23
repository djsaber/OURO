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
    return replace(
        prepared,
        tools=tools,
        commands=commands,
        continuation_policies=tuple(
            replace(policy, execution_contract_hash=execution_contract_hash)
            for policy in prepared.continuation_policies
        ),
        session_controllers=tuple(
            replace(controller, execution_contract_hash=execution_contract_hash)
            for controller in prepared.session_controllers
        ),
    )
