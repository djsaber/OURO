from __future__ import annotations

from pathlib import Path

from ouro.config.file import JsonConfigFile, atomic_config_update
from ouro.core.errors import ConfigError
from ouro.core.run_budget import RunBudgetPolicy


class RunBudgetConfigManager:
    def __init__(self, root: str | Path | None = None) -> None:
        self.file = JsonConfigFile(root)

    def snapshot(self) -> RunBudgetPolicy:
        raw = self.file.read().get("run_budget", {})
        if not isinstance(raw, dict):
            raise ConfigError("invalid run_budget config: expected object")
        try:
            return RunBudgetPolicy.model_validate(raw)
        except ValueError as exc:
            raise ConfigError(f"invalid run_budget config: {exc}") from exc

    @atomic_config_update
    def save(self, policy: RunBudgetPolicy) -> RunBudgetPolicy:
        try:
            validated = RunBudgetPolicy.model_validate(policy.model_dump())
        except ValueError as exc:
            raise ConfigError(f"invalid run_budget config: {exc}") from exc
        raw = self.file.read()
        serialized = validated.model_dump(mode="json", exclude_none=True)
        if serialized:
            raw["run_budget"] = serialized
        else:
            raw.pop("run_budget", None)
        self.file.write(raw)
        return validated

    @atomic_config_update
    def update(
        self,
        *,
        max_model_rounds: int | None = None,
        max_input_tokens: int | None = None,
        max_output_tokens: int | None = None,
        max_elapsed_seconds: float | None = None,
        clear: bool = False,
    ) -> RunBudgetPolicy:
        if clear:
            if any(
                value is not None
                for value in (
                    max_model_rounds,
                    max_input_tokens,
                    max_output_tokens,
                    max_elapsed_seconds,
                )
            ):
                raise ConfigError("clear cannot be combined with run budget limits")
            return self.save(RunBudgetPolicy())
        current = self.snapshot()
        updates = {
            key: value
            for key, value in {
                "max_model_rounds": max_model_rounds,
                "max_input_tokens": max_input_tokens,
                "max_output_tokens": max_output_tokens,
                "max_elapsed_seconds": max_elapsed_seconds,
            }.items()
            if value is not None
        }
        try:
            updated = RunBudgetPolicy.model_validate(
                current.model_copy(update=updates).model_dump()
            )
        except ValueError as exc:
            raise ConfigError(f"invalid run_budget config: {exc}") from exc
        return self.save(updated)


def run_budget_to_public_dict(policy: RunBudgetPolicy) -> dict[str, object]:
    return policy.model_dump(mode="json", exclude_none=True)
