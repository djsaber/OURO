"""Reconcile complete Responses items without reconstructing them from display deltas."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ouro.providers.responses_errors import ResponsesError


class ResponseItems:
    """Reconcile item events and optional final snapshots without rebuilding deltas."""

    def __init__(self) -> None:
        self._seen: dict[int, tuple[str, str | None]] = {}
        self._positions: dict[str, int] = {}
        self._completed: dict[int, dict[str, Any]] = {}

    def observe(self, index: Any, item: Any, *, complete: bool) -> None:
        if type(index) is not int or index < 0 or not isinstance(item, dict):
            raise ResponsesError("invalid_output_item")
        kind, item_id = item.get("type"), item.get("id")
        if not isinstance(kind, str) or not kind:
            raise ResponsesError("invalid_output_item")
        if item_id is not None and (not isinstance(item_id, str) or not item_id):
            raise ResponsesError("invalid_output_item")
        previous = self._seen.get(index)
        if previous is not None:
            if previous[0] != kind or (
                previous[1] is not None and item_id is not None and previous[1] != item_id
            ):
                raise ResponsesError("conflicting_output_item")
        if item_id is not None:
            if self._positions.get(item_id, index) != index:
                raise ResponsesError("conflicting_output_item")
            self._positions[item_id] = index
        self._seen[index] = (kind, item_id or (previous[1] if previous else None))
        if not complete:
            return
        if item.get("status") not in (None, "completed"):
            raise ResponsesError("unfinished_output_item")
        saved = self._completed.get(index, {})
        self._completed[index] = _merge_completed(saved, item)

    def finish(self, snapshot: Any) -> list[dict[str, Any]]:
        if not isinstance(snapshot, list):
            raise ResponsesError("invalid_output")
        for index, item in enumerate(snapshot):
            self.observe(index, item, complete=True)
        indices = sorted(self._seen)
        if any(index != position for position, index in enumerate(indices)) or any(
            index not in self._completed for index in indices
        ):
            raise ResponsesError("unfinished_output_item")
        # Preserve complete provider items (including reasoning ciphertext and
        # item ids). Delta text is display data, not another replay item.
        return deepcopy([self._completed[index] for index in indices])


def _merge_completed(saved: Any, incoming: Any) -> Any:
    """Fill missing fields at any depth without replacing completed content."""
    if isinstance(saved, dict) and isinstance(incoming, dict):
        result = deepcopy(saved)
        for key, value in incoming.items():
            result[key] = _merge_completed(saved[key], value) if key in saved else deepcopy(value)
        return result
    if isinstance(saved, list) and isinstance(incoming, list) and len(saved) == len(incoming):
        # Array positions identify content parts; appending, dropping or moving
        # parts would change completed content rather than supply metadata.
        return [_merge_completed(left, right) for left, right in zip(saved, incoming, strict=True)]
    if type(saved) is not type(incoming) or saved != incoming:
        raise ResponsesError("conflicting_output_item")
    return deepcopy(saved)
