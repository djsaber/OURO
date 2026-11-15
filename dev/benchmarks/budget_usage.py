"""Compare point and batch reads of the same immutable raw Run consumption."""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import tempfile
import tracemalloc
from time import perf_counter

from ouro.billing import LocalUsageLedger
from ouro.core.billing import ProviderUsageEntry
from ouro.core.state import RunRequestCounts
from ouro.core.usage_queries import RunUsageCoverage


def measure(call, repeat):
    samples = []
    for _ in range(repeat):
        started = perf_counter()
        call()
        samples.append((perf_counter() - started) * 1000)
    tracemalloc.start()
    call()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {"samples_ms": samples, "median_ms": statistics.median(samples), "peak_bytes": peak}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", nargs="+", type=int, default=[1, 100, 1000])
    parser.add_argument("--repeat", type=int, default=7)
    args = parser.parse_args()
    print(json.dumps({"python": platform.python_version(), "platform": platform.platform()}))
    with tempfile.TemporaryDirectory(prefix="ouro-budget-usage-") as directory:
        ledger = LocalUsageLedger(directory)
        for index in range(max(args.counts)):
            ledger.record(
                ProviderUsageEntry(
                    entry_id=f"usage_{index}",
                    provider_id="faux",
                    model_id="benchmark",
                    purpose="agent",
                    session_id="session",
                    run_id=f"run_{index}",
                    run_kind="agent",
                    request_id=f"request_{index}",
                    input_tokens=index + 1,
                    output_tokens=1,
                )
            )
        for count in args.counts:
            runs = tuple(
                RunRequestCounts(run_id=f"run_{i}", model_round_count=1, summary_request_count=0)
                for i in range(count)
            )

            def points(runs=runs):
                input_tokens = output_tokens = 0
                complete = True
                for run in runs:
                    value = ledger.run_usage("session", run.run_id)
                    if value is not None:
                        input_tokens += value.input_tokens
                        output_tokens += value.output_tokens
                    complete = complete and (
                        (value.agent_request_count if value else 0) >= run.model_round_count
                        and (value.compaction_request_count if value else 0)
                        >= run.summary_request_count
                    )
                return RunUsageCoverage(input_tokens, output_tokens, complete)

            def batch(runs=runs):
                return ledger.run_usage_coverage("session", runs)

            assert points() == batch()
            print(
                json.dumps(
                    {
                        "runs": count,
                        "point": measure(points, args.repeat),
                        "batch": measure(batch, args.repeat),
                    }
                ),
                flush=True,
            )


if __name__ == "__main__":
    main()
