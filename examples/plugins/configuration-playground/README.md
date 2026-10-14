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
then **Settings**. Common fields
use the shared text/number inputs and selection menus. Complex fields use JSON inputs with schema
examples. **Settings (JSON)** edits the complete object. **Unset field** removes an explicit value;
**Set null** retains an explicit null. Defaults are applied by the framework.

Press Enter on **Example key** to edit it inline. Tab switches between **KEY** for a saved key and **ENV**
to enter `env:OURO_CONFIGURATION_EXAMPLE_KEY`. A synthetic key is sufficient because the plugin
contacts no service. Environment references are checked without resolution when saving a disabled
plugin; enabling requires a nonempty value in that Host's environment. **Optional key** may remain
unbound. Credentials belong in these slots, separate from ordinary JSON values.

The same slot accepts either binding in an editing draft:

```json
{"credential_refs":{"example_key":"env:OURO_CONFIGURATION_EXAMPLE_KEY"}}
```

```json
{"credential_refs":{"example_key":{"type":"inline","value":"playground-test-key"}}}
```

Direct keys are stored as plaintext in a local file restricted to the current user. Ordinary query
and preview output masks them. An explicit `--export` preserves saved keys in the protected editing
file so it can be imported without replacing them with display masks.

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
