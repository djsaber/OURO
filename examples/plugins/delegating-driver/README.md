# Delegating Driver

This algorithm requests a task brief, starts two native `reader` Agents, reviews their results,
then either finishes or executes the model's tool batch. Every wait has a checkpoint identifying
the next step. Child sessions, shared budgets, tool permissions and result delivery belong to OURO.

Configure an Agent definition with ID `reader` and the tools appropriate for its tasks.
Use parallel tool execution and at least two child execution slots to run the readers concurrently.
Sequential Host execution is also supported. The plugin does not override Host concurrency limits.

After installing and enabling the plugin, select:

```python
await runtime.run(
    "Investigate this request and act on the evidence",
    driver_id="dev.ouro.examples.delegating_driver/run_drivers/delegate",
)
```

For worker execution, set the manifest runtime to:

```json
{"kind": "worker", "command": ["{python}", "worker.py"], "protocol": "3.0"}
```

Both entry points import only the public SDK. `RunAgents` submits typed controls through
the same native tool boundary as `RunTools.execute`, preserving call provenance without
creating artificial model tool calls.
