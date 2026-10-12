from __future__ import annotations

import json
from pathlib import Path

from ouro.core.harness import HarnessState
from ouro.core.harness_calls import HarnessCallSnapshot
from ouro.core.session_controls import RunControlBinding
from ouro.plugins.harness_calls import PluginStateView
from ouro.plugins.manifest import PluginManifest
from ouro.plugins.worker import WorkerPluginInstance
from ouro.sdk.plugins import v3 as sdk


async def test_worker_receives_native_usage_and_returns_a_controller_scope(tmp_path):
    package = Path(__file__).parents[2] / "examples/plugins/worker-tool"
    manifest = json.loads((package / ".ouro-plugin/plugin.json").read_text(encoding="utf-8"))
    manifest["contributes"]["session_controllers"] = [
        {
            "id": "mode",
            "description": "Own work",
            "claims": [{"domain": "example.mode", "access": "exclusive"}],
        }
    ]
    (tmp_path / "worker.py").write_text(
        """import json
import sys
for line in sys.stdin:
    request = json.loads(line)
    method = request['method']
    result = {}
    if method == 'initialize':
        result = {'protocol_version': '3.0'}
    elif method == 'contributions.prepare':
        result = {'tools': {'echo': {}}, 'session_controllers': ['mode']}
    elif method == 'session_controller.call':
        result = {'data': {**request['params']['session_state'],
                  'replacement': request['params']['call']['replacement_controller_id']},
                  'usage_scope': 'next-goal'}
    if isinstance(request.get('id'), int):
        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
    if method == 'shutdown':
        break
""",
        encoding="utf-8",
    )
    worker = WorkerPluginInstance(
        PluginManifest.model_validate(manifest),
        package_root=tmp_path,
        package_digest="test",
        permissions=frozenset(),
        configuration={},
        data_directory=tmp_path / "data",
        credentials={},
        diagnostic=lambda *_args: None,
    )
    owner = manifest["plugin_id"]
    view = PluginStateView(
        HarnessCallSnapshot(
            session_id="session",
            harness_version="harness",
            history_epoch=0,
            state=HarnessState(),
            run_controls=(
                RunControlBinding(
                    controller_id=f"{owner}/session_controllers/mode",
                    activation_id="old",
                    usage_scope="goal",
                ),
            ),
            usage_tokens={owner: {"goal": 23}, "another-owner": {"secret": 99}},
        ),
        owner,
        writable=True,
    )
    try:
        prepared = await worker.prepare()
        output = await prepared.session_controllers[0].activate(
            sdk.SessionControllerCall(
                session_id="session",
                session_kind="primary",
                activation_id="new",
                controller_id="mode",
                action="activate",
                session_state=view,
            )
        )
        assert output.usage_scope == "next-goal"
        assert output.data["usage_tokens"] == {"goal": 23}
        assert output.data["run_controls"] == [
            {"controller_id": "mode", "activation_id": "old", "usage_scope": "goal"}
        ]
        assert output.data["controls"] == []
        assert output.data["replacement"] is None
        deactivated = await prepared.session_controllers[0].deactivate(
            sdk.SessionControllerCall(
                session_id="session",
                session_kind="primary",
                activation_id="old",
                controller_id="mode",
                action="deactivate",
                session_state=view,
                replacement_controller_id=f"{owner}/session_controllers/mode",
            )
        )
        assert deactivated.data["replacement"] == f"{owner}/session_controllers/mode"
        assert view.finish() is None
    finally:
        view.close()
        await worker.stop("shutdown", 0)
