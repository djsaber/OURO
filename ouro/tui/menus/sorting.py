from __future__ import annotations

import re
import unicodedata

from ouro.application.contracts.agents import AgentDefinitionView
from ouro.application.contracts.models import AvailableModel


def display_sort_key(value: str | None, *, fallback: str) -> tuple[str, str]:
    normalized_fallback = unicodedata.normalize("NFKC", fallback).casefold()
    normalized_value = unicodedata.normalize("NFKC", (value or "").strip()).casefold()
    return (normalized_value or normalized_fallback, normalized_fallback)


def model_name_sort_key(
    display_name: str | None, *, model_id: str
) -> tuple[tuple[tuple[int, int | str], ...], str]:
    display_name = unicodedata.normalize("NFKC", (display_name or "").strip())
    # Localized labels remain visible; the model ID supplies a comparable version.
    name = display_name if display_name and display_name.isascii() else model_id
    normalized = unicodedata.normalize("NFKC", name).casefold()
    parts = tuple(
        (1, int(part)) if part.isdecimal() else (0, part)
        for part in re.findall(r"\d+|\D+", normalized)
    )
    return parts, unicodedata.normalize("NFKC", model_id).casefold()


def sorted_models(models: tuple[AvailableModel, ...]) -> tuple[AvailableModel, ...]:
    """Order providers by visible name and their model names largest first."""

    providers: dict[str, list[AvailableModel]] = {}
    for model in models:
        providers.setdefault(model.provider_id, []).append(model)
    return tuple(
        model
        for provider_models in sorted(
            providers.values(),
            key=lambda group: display_sort_key(
                group[0].provider_display_name, fallback=group[0].provider_id
            ),
        )
        for model in sorted(
            provider_models,
            key=lambda item: (
                model_name_sort_key(item.display_name, model_id=item.model_id),
                item.key.casefold(),
            ),
            reverse=True,
        )
    )


def sorted_agent_definitions(
    definitions: tuple[AgentDefinitionView, ...],
) -> tuple[AgentDefinitionView, ...]:
    return tuple(
        sorted(
            definitions,
            key=lambda item: (item.source != "builtin", item.name.casefold(), item.agent_id),
        )
    )
