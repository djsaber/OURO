# Model Review

The `model-review <default|provider/endpoint> <text>` command reviews text through the public model
port. It requires `models.request` and records provider-reported usage against this plugin. It does
not create a Session or Run. `default` requires a configured default endpoint.

Install and enable the package, then invoke
`/dev.ouro.examples.model_review:model-review default Review this proposal.`

The manifest selects the Python in-process entry. To use the supplied worker entry, set its runtime
to `{"kind":"worker","command":["{python}","worker.py"],"protocol":"3.0"}` before installation.
Both entries execute `ModelReview.command`; the worker uses only public SDK request and output routes.

The request has a 120-second deadline and the command allows 125 seconds. Plugin shutdown cancels
active requests. The Host settles known usage before returning a result. Use `ouro usage summary
--purpose plugin --group-by plugin` to inspect independent plugin usage.
