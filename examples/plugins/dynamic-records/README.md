# Dynamic records

This plugin publishes a complete `records` tool group. `RecordData` saves structured
records in the calling Session through the public SDK state view. The default field is
`title`; `/dev.ouro.examples.dynamic_records:record-fields title,category` publishes a new schema and
`/dev.ouro.examples.dynamic_records:record-fields off` withdraws the tool.

The manifest fixes the group's permission and execution policy. Each publication supplies
its tool schema and an implementation revision. A model request already in flight keeps
the schema and handler it received. Later decisions see the current group. Pending
approval after restart requires the original revision; startup publishes the default
`title` schema and does not persist runtime field selections.

The default runtime is a Python worker. `WorkerToolGroups` installs the call, resume and
release protocol routes and invokes one-argument SDK tool handlers, including their
Session state view. Handler references remain alive until the Host releases the
publication's decision leases. Release notifications do not occupy business request slots.

To run the same implementation in process, replace the manifest's `runtime` section:

```json
{
  "runtime": {
    "kind": "python-inprocess",
    "entrypoint": "dynamic_records_plugin:create_plugin"
  }
}
```
