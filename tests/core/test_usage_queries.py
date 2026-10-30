from datetime import UTC, datetime

import pytest

from ouro.core.billing import UsageFilter
from ouro.core.usage_queries import (
    RunUsageTotals,
    UsageAuditLog,
    UsageEntryCursor,
    UsageEntryList,
    UsageMutationResult,
    UsageTotals,
)


@pytest.mark.parametrize("model", [UsageTotals, RunUsageTotals])
def test_usage_totals_and_mutation_output_follow_base_counts(model):
    totals = model(input_tokens=10, output_tokens=5, request_count=2, priced_request_count=1)
    changed = totals.model_copy(update={"input_tokens": 20, "request_count": 3})
    result = UsageMutationResult(action="reset", affected_totals=changed)

    assert totals.total_tokens == 15
    assert totals.unpriced_request_count == 1
    assert changed.total_tokens == 25
    assert changed.unpriced_request_count == 2
    payload = result.model_dump(mode="json")
    assert payload["affected_count"] == 3
    assert payload["affected_totals"]["total_tokens"] == 25
    assert payload["affected_totals"]["unpriced_request_count"] == 2
    assert model.model_validate(changed.model_dump()).model_dump() == changed.model_dump()


@pytest.mark.parametrize(
    "page",
    [
        UsageEntryList(
            usage_filter=UsageFilter(),
            next_cursor=UsageEntryCursor(
                occurred_at=datetime(2026, 1, 1, tzinfo=UTC), entry_id="entry_next"
            ),
        ),
        UsageAuditLog(next_cursor=1),
    ],
)
def test_pagination_output_follows_cursor_without_separate_flag(page):
    assert page.truncated is True
    assert page.model_dump(mode="json")["truncated"] is True
    last_page = page.model_copy(update={"next_cursor": None})
    assert last_page.truncated is False
    assert last_page.model_dump(mode="json")["truncated"] is False
