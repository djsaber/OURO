"""Cold contract use preserves validation, immutability and serialization."""

import subprocess
import sys


def test_deferred_contracts_support_concurrent_first_use_and_schema_export():
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import ouro.tui.app
from pydantic import ValidationError
from ouro.application.contracts.base import ApplicationDTO, ApplicationStartupReport, Diagnostic

assert not Diagnostic.__pydantic_complete__
barrier = Barrier(8)
payload = {"diagnostics": [{"code": "startup", "severity": "warning", "component": "test",
    "message": "Optional dependency unavailable", "occurred_at": "2026-01-01T00:00:00Z"}]}

def first_use(_):
    barrier.wait(timeout=10)
    report = ApplicationStartupReport.model_validate(payload)
    restored = ApplicationStartupReport.model_validate_json(report.model_dump_json())
    assert restored == report
    assert report.diagnostics[0].severity == "warning"
    try:
        report.diagnostics[0].message = "changed"
    except ValidationError as exc:
        assert exc.errors()[0]["type"] == "frozen_instance"
    else:
        raise AssertionError("contract is mutable")

with ThreadPoolExecutor(max_workers=8) as pool:
    list(pool.map(first_use, range(8)))

try:
    Diagnostic(code="bad", severity="invalid", component="test", message="invalid")
except ValidationError as exc:
    assert exc.errors()[0]["type"] == "literal_error"
else:
    raise AssertionError("invalid contract was accepted")

pending = [ApplicationDTO]
seen = set()
while pending:
    model = pending.pop()
    if model in seen:
        continue
    seen.add(model)
    pending.extend(model.__subclasses__())
    model.model_json_schema()
""",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
