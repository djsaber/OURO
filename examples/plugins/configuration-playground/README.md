# Configuration Playground

This declarative plugin has no entrypoint, worker, contributions, network calls or business logic.
It exposes configuration fields for testing the framework contract and generic Host editors.
It requires Plugin API 3.1 or newer within major version 3.

The schema covers scalar and nullable values, typed enums, constants, nested defaults, dynamic and
pattern-based mappings, arrays, tuples, tagged unions, conditions, dependent fields, composition,
local references, recursive trees and unconstrained JSON. Credentials are separate declared slots.

Install from the repository root using PowerShell:

```powershell
uv run ouro plugin install ./examples/plugins/configuration-playground --home ./.ouro-dev
uv run ouro plugin configuration dev.ouro.configuration_playground --home ./.ouro-dev --export ./playground-draft.json
```

Start the TUI with `uv run ouro --home ./.ouro-dev`, open **Plugins**, choose **Configuration Playground**,
then **Edit settings**. Common fields
use the shared text/number inputs and selection menus. Complex fields use JSON inputs with schema
examples. **Settings (JSON)** edits the complete object. **Unset a field** removes an explicit value;
**Set a nullable field to null** retains an explicit null. Defaults are applied by the framework.

Bind **Example key** to `env:OURO_CONFIGURATION_EXAMPLE_KEY` before applying a draft. Saving while
disabled checks the reference without resolving it. Enabling requires a nonempty value in that Host's
environment; a synthetic value is sufficient because the plugin contacts no service. **Optional key**
may remain unbound. Never put a real secret in the JSON values or a reference field.

For file editing, retain the exported plugin ID, digest and expected revision. Edit `values` and
`credential_refs`, then validate and apply with PowerShell:

```powershell
uv run ouro plugin configure dev.ouro.configuration_playground --home ./.ouro-dev --file ./playground-draft.json --preview --json
uv run ouro plugin configure dev.ouro.configuration_playground --home ./.ouro-dev --file ./playground-draft.json --json
```

Try a negative `attempts`, duplicate `labels`, an extra tuple item, or `mode: "advanced"` without
`advanced_options`. Each must produce a field/structure validation issue and leave saved state intact.
Applying another draft makes an older exported baseline stale; re-export and review current values
before submitting again. Schema examples are editing hints and do not automatically populate fields.

The [configuration specification](../../../docs/specs/plugin-configuration.md) defines accepted
values, default materialization, credential handling, concurrency and runtime application.
