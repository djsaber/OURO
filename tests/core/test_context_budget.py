import pytest

from ouro.core.context import ContextBuilder, ContextMetrics
from ouro.core.context_budget import ContextBudget


@pytest.mark.parametrize("limit,capacity", [(None, 1000), (200, 800), (999, 1)])
def test_context_budget_uses_only_this_requests_limits(limit, capacity):
    budget = ContextBudget(context_window=1000, max_output_tokens=limit)
    view = ContextBuilder().build(messages=[], tools=[], budget=budget)
    assert view.metrics.input_budget_tokens == capacity
    assert view.metrics.estimated_remaining_tokens == capacity
    assert not view.metrics.over_budget
    assert ContextMetrics.model_validate_json(view.metrics.model_dump_json()) == view.metrics


@pytest.mark.parametrize("limit", [0, -1, 1000, 1001, True, 1.5])
def test_context_budget_rejects_invalid_limits_and_exhausted_capacity(limit):
    with pytest.raises(ValueError):
        ContextBudget(context_window=1000, max_output_tokens=limit)


def test_metrics_recalculate_from_measured_input_without_mutating_the_original():
    metrics = ContextMetrics(
        estimated_input_tokens=900,
        estimated_fixed_tokens=100,
        budget=ContextBudget(context_window=1000),
    )
    updated = metrics.with_budget(ContextBudget(context_window=1000, max_output_tokens=200))
    assert updated.estimated_input_tokens == metrics.estimated_input_tokens
    assert updated.estimated_remaining_tokens == -100
    assert updated.estimated_utilization_ratio == 1.125
    assert updated.over_budget
    assert metrics.estimated_remaining_tokens == 100
    assert not metrics.over_budget
