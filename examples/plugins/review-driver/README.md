# Review Driver

This plugin owns a Run algorithm: review without tools, request an action, then execute the
model's tool batch through the native port. An approval or interaction wait stores a small
checkpoint; the resumed invocation receives the settled native results without rerunning the review.

The manifest selects the in-process entry point. To run the same algorithm in a worker, set:

```json
{
  "runtime": {"kind": "worker", "command": ["{python}", "worker.py"], "protocol": "3.0"}
}
```

After installing and enabling the plugin, use `/review <request>` in a Host that supports
plugin commands. The command returns a public SDK `SessionInput` selecting this Driver;
OURO accepts it into the Session queue and binds the installed implementation and dependencies.
An upgrade does not change an already accepted input's algorithm. Busy Sessions consume it
as a follow-up Run. A Host can also select the algorithm directly:

```python
await runtime.run(
    "Review and carry out this request",
    driver_id="dev.ouro.examples.review_driver/run_drivers/review",
)
```

The plugin imports only the public SDK. Model routing, context, budgets, tool permissions,
waiting state and Run settlement remain owned by OURO. The worker uses `WorkerRunDrivers`
and supports Host callbacks while its single business slot is occupied by the Driver.
