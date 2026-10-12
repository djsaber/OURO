# Native Review Service

This plugin exports two review operations for a caller that owns a live Run. `inspect` requests
a review without tools. `act` requests an action and submits any model tools to OURO's native
approval and checkpoint boundary. The service uses only `PluginContext.models` and `.tools`.
The methods declare `state_changing` because model calls change Run history, counters and usage.

Install and enable this package, and declare it as a dependency of the consuming plugin:

```json
{
  "dependencies": {
    "plugins": [{"plugin_id": "dev.ouro.examples.review_service", "version": "^0.1.0"}]
  }
}
```

Within a Driver, use the `services` port received by the consuming plugin's factory:

```python
if resume is not None:
    return sdk.DriverFinished("\n".join(item.content for item in resume.results))
review = await services.call("dev.ouro.examples.review_service", "review", "inspect")
action = await services.call("dev.ouro.examples.review_service", "review", "act", review["text"])
if action["suspended"]:
    return sdk.DriverSuspended()
return sdk.DriverFinished(action["text"], response_id=action.get("response_id"))
```

The calling Run owns model budgets, usage, tool permissions and waiting results. The service
does not create another Run or retain a live coroutine during approval. The Driver receives
the checkpoint and native results on resume. Calls without a live Run, read-only calls and
reentry during another native operation fail before starting an operation.

To run this service in a separate worker, set its manifest runtime to:

```json
{
  "runtime": {"kind": "worker", "command": ["{python}", "worker.py"], "protocol": "3.0"}
}
```

The worker uses `WorkerRunPorts`; the Host binds each reverse request to its incoming service
call. Neither the service nor the worker supplies a Run identity to native ports. Cancellation
follows the calling Run and plugin generation; ordinary RPC defaults do not shorten model work.
