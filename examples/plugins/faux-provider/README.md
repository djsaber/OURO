# Faux model protocol

This package implements the deterministic Faux response using the public Provider SDK. It owns
the stream and connection check; OURO owns model routes, model requests, cancellation and usage.
It requires no external service or credentials. It is a registration example, not an inference engine.

```shell
uv run --frozen ouro plugin install examples/plugins/faux-provider --home .ouro-dev
uv run --frozen ouro plugin enable dev.ouro.examples.faux_provider --home .ouro-dev
uv run --frozen ouro provider add "Plugin Faux" --type custom --protocol dev.ouro.examples.faux_provider/providers/faux --home .ouro-dev
uv run --frozen ouro provider endpoint add <provider_id> --model faux --default --home .ouro-dev
uv run --frozen ouro provider check <provider_id> --home .ouro-dev
uv run --frozen ouro run --provider <provider_id> --home .ouro-dev "Hello"
uv run --frozen ouro plugin disable dev.ouro.examples.faux_provider --home .ouro-dev
uv run --frozen ouro plugin remove dev.ouro.examples.faux_provider --home .ouro-dev
```

Use the Provider ID printed by `provider add`. The model route remains configured after removing
the plugin and reports that its protocol is unavailable. Removing the plugin does not delete
configuration, conversations or usage.

For worker execution, set the manifest runtime to:

```json
{"kind": "worker", "command": ["{python}", "worker.py"], "protocol": "3.0"}
```

Both entries use the same `ProviderContribution` implementation. The worker uses a root concurrency
limit of one and awaits each output callback. Provider implementations must await output delivery,
emit canonical usage only with `done`, and settle their request resources before returning.
Clients shared between requests belong to the plugin instance and must close in `stop`.
