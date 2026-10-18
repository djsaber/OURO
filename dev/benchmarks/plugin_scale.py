"""Measure plugin startup, calls and management at explicit package/process counts."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import platform
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path
from time import perf_counter

import ouro
from dev.benchmarks.harness_lifecycle import process_io
from dev.benchmarks.plugin_lifecycle import process_handles
from ouro.local_application import open_local_plugins
from ouro.plugins.installer import PluginInstaller
from ouro.plugins.store import PluginPackageStore

_PLUGIN = """from ouro.sdk.plugins.v4 import CommandContribution, ContributionSet

class Plugin:
    async def prepare(self):
        async def echo(call):
            return call.argument
        return ContributionSet(commands=[
            CommandContribution(id="echo", description="Echo benchmark.", handler=echo)
        ])
    async def start(self): pass
    async def stop(self, reason, deadline): pass

def create_plugin(context): return Plugin()
"""

_WORKER = """import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    result = {}
    if method == "initialize":
        result = {"protocol_version": "3.0"}
    elif method == "contributions.prepare":
        result = {"commands": ["echo"]}
    elif method == "command.call":
        result = {"output": request["params"]["call"]["argument"]}
    if isinstance(request.get("id"), int):
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
    if method == "shutdown":
        break
"""


def prepare_home(root: Path, kind: str, count: int) -> Path:
    home = root / "home"
    store = PluginPackageStore(home)
    installer = PluginInstaller(store)
    for index in range(count):
        source = root / f"source-{index}"
        (source / ".ouro-plugin").mkdir(parents=True)
        runtime: dict[str, object] = {"kind": kind}
        if kind == "declarative":
            skill = source / f"probe-{index}"
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                f"---\nname: probe-{index}\ndescription: Offline benchmark.\n---\nProbe.\n",
                encoding="utf-8",
            )
            contributes = {"skills": [{"id": f"probe-{index}", "path": f"./probe-{index}"}]}
        else:
            contributes = {"commands": [{"id": "echo", "description": "Echo benchmark."}]}
            if kind == "python-inprocess":
                runtime["entrypoint"] = f"probe_{index}_plugin:create_plugin"
                (source / f"probe_{index}_plugin.py").write_text(_PLUGIN, encoding="utf-8")
            else:
                runtime.update(command=["{python}", "./worker.py"], protocol="3.0")
                (source / "worker.py").write_text(_WORKER, encoding="utf-8")
        manifest = {
            "schema_version": 3,
            "plugin_id": f"dev.benchmark.p{index}",
            "name": f"Probe {index}",
            "version": "1.0.0",
            "namespace": f"probe_{index}",
            "engines": {"ouro": ">=0.3.0,<0.4.0", "plugin_api": "^4.0.0"},
            "runtime": runtime,
            "contributes": contributes,
        }
        (source / ".ouro-plugin" / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        installer.install(source)
    # Fixture setup only: publish all enable intents before measuring a new Host.
    expected = store.read_state()
    enabled = expected.model_copy(deep=True)
    for record in enabled.plugins.values():
        record.activation.desired_enabled = True
    store.commit_candidate_state(expected, enabled)
    return home


def delta(before: dict[str, int]) -> dict[str, int]:
    after = process_io()
    return {key: after[key] - value for key, value in before.items()}


def distribution(values: list[float]) -> dict[str, object]:
    ordered = sorted(values)
    return {
        "samples_ms": values,
        "p50_ms": statistics.median(values),
        "p95_ms": ordered[math.ceil(len(ordered) * 0.95) - 1],
    }


async def sample(home: Path, kind: str, count: int) -> dict[str, object]:
    report: dict[str, object] = {
        "kind": kind,
        "user_plugins": count,
        "source_root": str(Path(ouro.__file__).resolve().parent.parent),
    }
    before_io = process_io()
    before_handles = process_handles()
    started = perf_counter()
    async with open_local_plugins(home, session_backend="files") as plugins:
        report["startup_ms"] = (perf_counter() - started) * 1000
        report["startup_io"] = delta(before_io)
        runtime = plugins.runtime
        assert runtime is not None
        assert sum(key.startswith("dev.benchmark.") for key in runtime._active) == count
        assert all(item.status == "active" for item in plugins.list())
        report["active_generations"] = len(runtime._active)
        report["active_workers"] = sum(
            item.record.runtime_kind == "worker" for item in runtime._active.values()
        )
        report["handles_before"] = before_handles
        report["handles_active"] = process_handles()
        io_before_calls = process_io()
        # Declarative packages expose metadata. Executable packages perform a real call.
        samples = []
        for _ in range(21):
            started = perf_counter()
            if count and kind != "declarative":
                result = await plugins.execute_command("dev.benchmark.p0:echo", "probe")
                assert result.output == "probe"
            else:
                await runtime.synchronize()
                assert len(runtime.catalog().skills) >= count
            samples.append((perf_counter() - started) * 1000)
        report["first_use_ms"] = samples[0]
        report["steady_use"] = distribution(samples[1:])
        report["calls_io"] = delta(io_before_calls)
        io_before_list = process_io()
        started = perf_counter()
        plugins.list()
        report["list_ms"] = (perf_counter() - started) * 1000
        report["list_io"] = delta(io_before_list)
        if count:
            io_before_cycle = process_io()
            started = perf_counter()
            disabled = await plugins.disable("dev.benchmark.p0")
            assert disabled.status == "disabled", disabled
            enabled = await plugins.enable("dev.benchmark.p0")
            assert enabled.status == "active", (enabled, plugins.diagnostics())
            report["disable_enable_ms"] = (perf_counter() - started) * 1000
            report["disable_enable_io"] = delta(io_before_cycle)
        started = perf_counter()
    report["shutdown_ms"] = (perf_counter() - started) * 1000
    report["handles_closed"] = process_handles()
    assert not runtime._active
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", type=int, nargs="+", default=[0, 10, 100])
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument(
        "--kinds",
        nargs="+",
        choices=["declarative", "python-inprocess", "worker"],
        default=["declarative", "python-inprocess", "worker"],
    )
    parser.add_argument("--sample-home", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.repeat < 1 or any(count < 0 for count in args.counts):
        parser.error("repeat must be positive and counts must be nonnegative")
    if args.sample_home is not None:
        print(json.dumps(asyncio.run(sample(args.sample_home, args.kinds[0], args.counts[0]))))
        return
    print(json.dumps({"platform": platform.platform(), "python": platform.python_version()}))
    with tempfile.TemporaryDirectory(prefix="ouro-plugin-scale-") as directory:
        for kind in args.kinds:
            for count in args.counts:
                home = prepare_home(Path(directory) / f"{kind}-{count}", kind, count)
                reports = []
                for index in range(args.repeat):
                    result = subprocess.run(
                        [
                            sys.executable,
                            "-X",
                            "utf8",
                            "-m",
                            "dev.benchmarks.plugin_scale",
                            "--sample-home",
                            str(home),
                            "--kinds",
                            kind,
                            "--counts",
                            str(count),
                        ],
                        check=False,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                    )
                    if result.returncode:
                        raise RuntimeError(result.stderr)
                    report = json.loads(result.stdout)
                    if Path(report["source_root"]) != Path(ouro.__file__).resolve().parent.parent:
                        raise RuntimeError("scale probe imported a different checkout")
                    reports.append(report)
                    print(json.dumps({"sample": index + 1, **report}), flush=True)
                print(
                    json.dumps(
                        {
                            "summary": True,
                            "kind": kind,
                            "user_plugins": count,
                            **{
                                key: distribution([item[key] for item in reports])
                                for key in ("startup_ms", "first_use_ms", "list_ms", "shutdown_ms")
                            },
                        }
                    ),
                    flush=True,
                )


if __name__ == "__main__":
    main()
