"""Collect complete Responses items without reconstructing them from display deltas."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ouro.providers.openai_responses.errors import OpenAIResponsesError


class OpenAIResponsesItemAccumulator:
    """Keep each first complete item; terminal snapshots supply missing items."""

    def __init__(self) -> None:
        self._seen: dict[int, tuple[str, str | None]] = {}
        self._positions: dict[str, int] = {}
        self._completed: dict[int, dict[str, Any]] = {}

    @property
    def completed_indices(self) -> frozenset[int]:
        """Snapshot of positions whose complete items have already been accepted."""
        return frozenset(self._completed)

    def observe(self, index: Any, item: Any, *, complete: bool) -> bool:
        """Accept an observation, returning false for a position already complete."""
        if type(index) is not int or index < 0 or not isinstance(item, dict):
            raise OpenAIResponsesError("invalid_output_item")
        if index in self._completed:
            return False
        kind, item_id = item.get("type"), item.get("id")
        if not isinstance(kind, str) or not kind:
            raise OpenAIResponsesError("invalid_output_item")
        if item_id is not None and (not isinstance(item_id, str) or not item_id):
            raise OpenAIResponsesError("invalid_output_item")
        previous = self._seen.get(index)
        if previous is not None:
            if previous[0] != kind or (
                previous[1] is not None and item_id is not None and previous[1] != item_id
            ):
                raise OpenAIResponsesError(
                    "conflicting_output_item", detail=f"item_identity, output_index={index}"
                )
        if item_id is not None:
            if self._positions.get(item_id, index) != index:
                raise OpenAIResponsesError(
                    "conflicting_output_item", detail=f"item_position, output_index={index}"
                )
            self._positions[item_id] = index
        self._seen[index] = (kind, item_id or (previous[1] if previous else None))
        if not complete:
            return True
        if item.get("status") not in (None, "completed"):
            raise OpenAIResponsesError(
                "unfinished_output_item", detail=f"item_status, output_index={index}"
            )
        self._completed[index] = deepcopy(item)
        return True

    def finish(self, snapshot: Any) -> list[dict[str, Any]]:
        if not isinstance(snapshot, list):
            raise OpenAIResponsesError("invalid_output")
        for index, item in enumerate(snapshot):
            self.observe(index, item, complete=True)
        indices = sorted(self._seen)
        if any(index != position for position, index in enumerate(indices)) or any(
            index not in self._completed for index in indices
        ):
            raise OpenAIResponsesError("unfinished_output_item")
        # Preserve complete provider items (including reasoning ciphertext and
        # item ids). Delta text is display data, not another replay item.
        return deepcopy([self._completed[index] for index in indices])
