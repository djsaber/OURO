"""Read shared Agent budgets from canonical Run counts and the usage ledger."""

from __future__ import annotations

from dataclasses import dataclass

from ouro.application.usage import UsageService
from ouro.core.base import utc_now
from ouro.core.errors import PersistenceError, SessionBusyError
from ouro.core.run_budget import RunBudgetPolicy, RunPauseReason
from ouro.core.state import RunRecord
from ouro.sessions import SessionStore


@dataclass(frozen=True)
class _RootBudgetState:
    policy: RunBudgetPolicy
    input_tokens: int
    output_tokens: int
    model_rounds: int
    elapsed_seconds: float
    usage_known: bool


def effective_child_budget(
    store: SessionStore,
    usage: UsageService | None,
    *,
    root_session_id: str,
    parent_run: RunRecord,
    requested: RunBudgetPolicy,
) -> RunBudgetPolicy:
    if (
        requested.max_input_tokens is not None or requested.max_output_tokens is not None
    ) and usage is None:
        raise ValueError("Child token consumption budgets require a usage ledger")
    state = _root_budget_state(store, usage, root_session_id, parent_run)
    if state is None:
        return requested
    if not state.usage_known:
        raise SessionBusyError("Root token consumption is unknown")

    def remaining_int(limit: int | None, used: int, name: str) -> int | None:
        if limit is None:
            return None
        remaining = limit - used
        if remaining < 1:
            raise SessionBusyError(f"root {name} budget is exhausted")
        return remaining

    remaining_elapsed: float | None = None
    if state.policy.max_elapsed_seconds is not None:
        remaining_elapsed = state.policy.max_elapsed_seconds - state.elapsed_seconds
        if remaining_elapsed <= 0:
            raise SessionBusyError("root elapsed time budget is exhausted")

    remaining = RunBudgetPolicy(
        max_model_rounds=remaining_int(
            state.policy.max_model_rounds,
            state.model_rounds,
            "model round",
        ),
        max_input_tokens=remaining_int(
            state.policy.max_input_tokens,
            state.input_tokens,
            "input token",
        ),
        max_output_tokens=remaining_int(
            state.policy.max_output_tokens,
            state.output_tokens,
            "output token",
        ),
        max_elapsed_seconds=remaining_elapsed,
    )
    return requested.constrained_by(remaining)


def shared_budget_reason(
    store: SessionStore,
    usage: UsageService | None,
    run: RunRecord,
) -> RunPauseReason | None:
    """Recheck a shared root budget at each ordinary model boundary."""

    session = store.load_session_record(run.session_id)
    if session.session_kind == "subagent":
        root_session_id = session.root_session_id
        if root_session_id is None:
            raise PersistenceError(f"subagent Session {session.session_id} has no root Session")
        relation = store.find_delegation_by_child_session(
            root_session_id,
            run.session_id,
        )
        if relation is None:
            raise PersistenceError(
                f"subagent Session {session.session_id} has no delegation relation"
            )
        parent_run = store.load_run(
            relation.parent_session_id,
            relation.parent_run_id,
        )
    else:
        parent_run = store.load_run(run.session_id, run.run_id)
        root_session_id = session.agent_root_session_id
    state = _root_budget_state(store, usage, root_session_id, parent_run)
    if state is None:
        return None
    if not state.usage_known:
        return "usage_unavailable"
    return state.policy.exceeded_reason(
        next_model_round=state.model_rounds + 1,
        input_tokens=state.input_tokens,
        output_tokens=state.output_tokens,
        elapsed_seconds=state.elapsed_seconds,
    )


def _root_budget_state(
    store: SessionStore,
    usage: UsageService | None,
    root_session_id: str,
    parent_run: RunRecord,
) -> _RootBudgetState | None:
    raw = parent_run.metadata.get("run_budget")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("parent Run has invalid budget metadata")
    policy = RunBudgetPolicy.model_validate(raw)
    if (
        policy.max_input_tokens is not None or policy.max_output_tokens is not None
    ) and usage is None:
        raise ValueError("Shared token consumption budgets require a usage ledger")
    if all(
        limit is None
        for limit in (
            policy.max_model_rounds,
            policy.max_input_tokens,
            policy.max_output_tokens,
            policy.max_elapsed_seconds,
        )
    ):
        return None
    latest_parent = store.load_run(parent_run.session_id, parent_run.run_id)
    token_limited = policy.max_input_tokens is not None or policy.max_output_tokens is not None
    parent_usage = (
        usage.run_usage(latest_parent.session_id, latest_parent.run_id)
        if usage is not None and token_limited
        else None
    )
    usage_known = not token_limited or (
        (parent_usage.agent_request_count if parent_usage is not None else 0)
        >= latest_parent.model_round_count
        and (parent_usage.compaction_request_count if parent_usage is not None else 0)
        >= latest_parent.summary_request_count
    )
    input_tokens = parent_usage.input_tokens if parent_usage is not None else 0
    output_tokens = parent_usage.output_tokens if parent_usage is not None else 0
    model_rounds = latest_parent.model_round_count
    seen_runs = {(latest_parent.session_id, latest_parent.run_id)}
    relations = (
        store.list_delegations(root_session_id, parent_run_id=parent_run.run_id)
        if token_limited or policy.max_model_rounds is not None
        else ()
    )
    for relation in relations:
        if relation.child_session_id is None:
            continue
        for child_run in store.load_run_request_counts(relation.child_session_id):
            identity = (relation.child_session_id, child_run.run_id)
            if identity in seen_runs:
                continue
            seen_runs.add(identity)
            child_usage = (
                usage.run_usage(relation.child_session_id, child_run.run_id)
                if usage is not None and token_limited
                else None
            )
            usage_known = usage_known and (
                not token_limited
                or (
                    (child_usage.agent_request_count if child_usage is not None else 0)
                    >= child_run.model_round_count
                    and (child_usage.compaction_request_count if child_usage is not None else 0)
                    >= child_run.summary_request_count
                )
            )
            if child_usage is not None:
                input_tokens += child_usage.input_tokens
                output_tokens += child_usage.output_tokens
            model_rounds += child_run.model_round_count
    return _RootBudgetState(
        policy=policy,
        usage_known=usage_known,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        model_rounds=model_rounds,
        elapsed_seconds=max(
            0.0,
            (utc_now() - latest_parent.started_at).total_seconds(),
        ),
    )
