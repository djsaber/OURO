"""Supply route facts from the model actually selected for an invocation."""

from __future__ import annotations

from ouro.core.provider import ModelMetadata
from ouro.core.request_context import ProviderRequestContext


def model_request_context(
    model: ModelMetadata,
    supplied: ProviderRequestContext | None,
    *,
    purpose: str | None = None,
) -> ProviderRequestContext:
    values = dict(supplied.values) if supplied is not None else {}
    # Route facts belong to the selected model, including route changes during
    # a run. A caller cannot accidentally send an earlier model's identity.
    values.update({"provider.id": model.provider_id, "model.id": model.model_id})
    values.pop("endpoint.id", None)
    if model.endpoint_id is not None:
        values["endpoint.id"] = model.endpoint_id
    if purpose is not None:
        values["request.purpose"] = purpose
    else:
        values.setdefault("request.purpose", "inference")
    return ProviderRequestContext(values)
