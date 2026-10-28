from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ouro.application.usage import SummaryRequestRecorder
from ouro.config.compaction import CompactionModelConfig
from ouro.core.adapter import RuntimeAdapter
from ouro.core.base import new_id
from ouro.core.callbacks import run_blocking
from ouro.core.cancellation import AbortSignal
from ouro.core.compaction import (
    CompactionAbortedError,
    CompactionArtifact,
    CompactionModelOutput,
    CompactionRequest,
    CompactionStrategyError,
    compaction_frames,
)
from ouro.core.context import (
    ContextBuilder,
    ContextView,
    estimate_text_tokens,
    estimate_text_tokens_from_character_counts,
    text_character_counts,
)
from ouro.core.errors import (
    CompactionError,
    ConfigError,
    ContextOverflowError,
)
from ouro.core.message_semantics import (
    escape_semantic_envelope_delimiters,
)
from ouro.core.messages import MessageRecord, ModelMessage, model_visible_message
from ouro.core.model_execution import ModelExecutionEvent, execute_model_request
from ouro.core.provider import (
    ProviderCompletion,
    ProviderStreamEvent,
    Usage,
)
from ouro.core.request_context import ProviderRequestContext
from ouro.prompts import SUMMARIZATION_PROMPT

_SUMMARY_PATTERN = re.compile(r"<summary>\s*(.*?)\s*</summary>", re.DOTALL)
_SUMMARY_INPUT_TAG = "conversation-to-summarize"


@dataclass(frozen=True)
class _CompactionAdapterRoute:
    adapter: RuntimeAdapter
    route: str


@dataclass(frozen=True)
class _SummaryChunkResult:
    summary: str
    usage: dict[str, int | float]
    model_output: CompactionModelOutput


class LLMSummaryCompactionStrategy:
    """Generate an incremental summary without entering the normal agent loop."""

    strategy_id = "builtin.llm_summary"
    version = "1"
    prompt_version = "4"

    def __init__(
        self,
        adapter: RuntimeAdapter,
        prompt: str = SUMMARIZATION_PROMPT,
        *,
        configured_adapter: RuntimeAdapter | None = None,
        configured_adapter_error: str | None = None,
        configured_adapter_resolver: Callable[[], RuntimeAdapter] | None = None,
        model_config: CompactionModelConfig | None = None,
        runtime_adapter_resolver: Callable[[str], RuntimeAdapter] | None = None,
        prepare_requests: Callable[[str, str], SummaryRequestRecorder] | None = None,
    ) -> None:
        self.runtime_adapter = adapter
        self.runtime_adapter_resolver = runtime_adapter_resolver
        self.configured_adapter = configured_adapter
        self.configured_adapter_error = configured_adapter_error
        self.configured_adapter_resolver = configured_adapter_resolver
        self.model_config = model_config or CompactionModelConfig()
        self.prepare_requests = prepare_requests
        self.prompt = prompt

    async def compact(
        self,
        request: CompactionRequest,
        *,
        signal: AbortSignal | None = None,
    ) -> CompactionArtifact:
        try:
            request_recorder = (
                await run_blocking(self.prepare_requests, request.session_id, request.run_id)
                if self.prepare_requests is not None and request.run_id is not None
                else None
            )
        except Exception as exc:
            raise CompactionStrategyError(
                f"cannot bind summary usage: {exc}", terminal=True
            ) from exc
        failures: list[str] = []
        model_outputs: list[CompactionModelOutput] = []
        if self.configured_adapter_error:
            failures.append(self.configured_adapter_error)

        routes = await run_blocking(self._adapter_routes, request.session_id)
        for route in routes:
            try:
                adapter = _apply_compaction_model_config(route.adapter, self.model_config)
                artifact = await self._compact_with_adapter(
                    request,
                    adapter=adapter,
                    route=route.route,
                    signal=signal,
                    request_recorder=request_recorder,
                )
            except Exception as exc:  # noqa: BLE001 - model-route fallback boundary.
                model_outputs.extend(_outputs_from_error(exc))
                if signal is not None and signal.is_aborted():
                    raise CompactionAbortedError(
                        "summary compaction was aborted",
                        model_outputs=model_outputs,
                    ) from exc
                if isinstance(exc, CompactionStrategyError) and exc.terminal:
                    raise CompactionStrategyError(
                        str(exc),
                        model_outputs=model_outputs,
                        terminal=True,
                    ) from exc
                failures.append(f"{route.route}: {type(exc).__name__}: {exc}")
                continue
            prior_outputs = list(model_outputs)
            model_outputs.extend(artifact.model_outputs)
            metadata = dict(artifact.metadata)
            metadata["model_route"] = route.route
            metadata["model_request_count"] = len(model_outputs)
            if failures:
                metadata["model_fallback_reason"] = "; ".join(failures)
            total_usage = dict(artifact.usage)
            for output in prior_outputs:
                total_usage = _merge_usage(total_usage, output.usage)
            return artifact.model_copy(
                update={
                    "usage": total_usage,
                    "metadata": metadata,
                    "model_outputs": model_outputs,
                }
            )

        message = "all compaction model routes failed: " + "; ".join(failures)
        if model_outputs:
            raise CompactionStrategyError(message, model_outputs=model_outputs)
        raise CompactionError(message)

    def _adapter_routes(self, session_id: str) -> list[_CompactionAdapterRoute]:
        routes: list[_CompactionAdapterRoute] = []
        try:
            configured = (
                self.configured_adapter_resolver()
                if self.configured_adapter_resolver is not None
                else self.configured_adapter
            )
        except ConfigError as exc:
            raise CompactionStrategyError(str(exc), terminal=True) from exc
        if configured is not None:
            routes.append(
                _CompactionAdapterRoute(
                    adapter=configured,
                    route="configured",
                )
            )
        runtime_route = "runtime_fallback" if self.model_config.has_configured_route else "runtime"
        runtime_adapter = (
            self.runtime_adapter_resolver(session_id)
            if self.runtime_adapter_resolver is not None
            else self.runtime_adapter
        )
        routes.append(
            _CompactionAdapterRoute(
                adapter=runtime_adapter,
                route=runtime_route,
            )
        )
        return routes

    async def _compact_with_adapter(
        self,
        request: CompactionRequest,
        *,
        adapter: RuntimeAdapter,
        route: str,
        signal: AbortSignal | None,
        request_recorder: SummaryRequestRecorder | None,
    ) -> CompactionArtifact:
        rolling_summary = request.previous_summary
        total_usage: dict[str, int | float] = {}
        model_outputs: list[CompactionModelOutput] = []
        remaining_frames = compaction_frames(request.messages)
        if not remaining_frames:
            raise CompactionError("summary compaction received no canonical messages")

        chunk_count = 0
        try:
            while remaining_frames:
                chunk, consumed_frames = self._next_chunk(
                    remaining_frames,
                    adapter=adapter,
                    agent_system_prompt=request.agent_system_prompt,
                    previous_summary=rolling_summary,
                )
                try:
                    result = await self._summarize_chunk(
                        adapter=adapter,
                        route=route,
                        chunk_index=chunk_count + 1,
                        attempt_index=1,
                        input_frame_count=consumed_frames,
                        session_id=request.session_id,
                        run_id=request.run_id,
                        agent_system_prompt=request.agent_system_prompt,
                        previous_summary=rolling_summary,
                        messages=chunk,
                        signal=signal,
                        request_recorder=request_recorder,
                    )
                except CompactionStrategyError as first_error:
                    if not _should_retry_smaller_chunk(first_error, consumed_frames):
                        raise
                    model_outputs.extend(first_error.model_outputs)
                    for output in first_error.model_outputs:
                        total_usage = _merge_usage(total_usage, output.usage)
                    consumed_frames = max(1, consumed_frames // 2)
                    chunk = [
                        message for frame in remaining_frames[:consumed_frames] for message in frame
                    ]
                    try:
                        result = await self._summarize_chunk(
                            adapter=adapter,
                            route=route,
                            chunk_index=chunk_count + 1,
                            attempt_index=2,
                            input_frame_count=consumed_frames,
                            session_id=request.session_id,
                            run_id=request.run_id,
                            agent_system_prompt=request.agent_system_prompt,
                            previous_summary=rolling_summary,
                            messages=chunk,
                            signal=signal,
                            request_recorder=request_recorder,
                        )
                    except Exception as retry_error:
                        raise CompactionStrategyError(
                            str(retry_error),
                            model_outputs=_outputs_from_error(retry_error),
                            terminal=True,
                        ) from retry_error
                rolling_summary = result.summary
                total_usage = _merge_usage(total_usage, result.usage)
                model_outputs.append(result.model_output)
                chunk_count += 1
                remaining_frames = remaining_frames[consumed_frames:]
        except Exception as exc:
            outputs = [*model_outputs, *_outputs_from_error(exc)]
            if outputs:
                raise CompactionStrategyError(
                    str(exc),
                    model_outputs=outputs,
                    terminal=isinstance(exc, CompactionStrategyError) and exc.terminal,
                ) from exc
            raise

        return CompactionArtifact(
            summary=rolling_summary or "",
            usage=total_usage,
            model_outputs=model_outputs,
            metadata={
                "prompt_version": self.prompt_version,
                "chunk_count": chunk_count,
                "model_request_count": len(model_outputs),
                "model_provider": adapter.model.provider_id,
                "model_endpoint_id": adapter.model.endpoint_id,
                "model_id": adapter.model.model_id,
                "model_temperature": adapter.model.temperature,
                "model_max_output_tokens": adapter.model.max_output_tokens,
                "model_reasoning": adapter.model.reasoning_resolution.effective.model_dump(
                    mode="json"
                ),
            },
        )

    def _next_chunk(
        self,
        frames: Sequence[Sequence[MessageRecord]],
        *,
        adapter: RuntimeAdapter,
        agent_system_prompt: str,
        previous_summary: str | None,
    ) -> tuple[list[MessageRecord], int]:
        """Choose one provider-safe chunk using the latest rolling summary size."""

        capacity = adapter.model.context_budget.input_tokens
        empty_context = self._summary_context(adapter, agent_system_prompt, previous_summary, [])
        empty_payload = empty_context.provider_context.messages[-1].text_content
        fixed_tokens = empty_context.metrics.estimated_input_tokens - estimate_text_tokens(
            empty_payload
        )
        ascii_characters, non_ascii_characters = text_character_counts(empty_payload)
        if (
            fixed_tokens
            + estimate_text_tokens_from_character_counts(
                ascii_characters=ascii_characters,
                non_ascii_characters=non_ascii_characters,
            )
            >= capacity
        ):
            raise ContextOverflowError(
                "summary instructions, Agent background and previous summary "
                "exceed model input budget"
            )

        current: list[MessageRecord] = []
        consumed_frames = 0
        has_serialized_messages = False
        for frame in frames:
            serialized_frame = escape_semantic_envelope_delimiters(
                _serialize_messages(frame), _SUMMARY_INPUT_TAG
            )
            separator = "\n" if serialized_frame and has_serialized_messages else ""
            added_ascii, added_non_ascii = text_character_counts(f"{separator}{serialized_frame}")
            if (
                fixed_tokens
                + estimate_text_tokens_from_character_counts(
                    ascii_characters=ascii_characters + added_ascii,
                    non_ascii_characters=non_ascii_characters + added_non_ascii,
                )
                > capacity
            ):
                if current:
                    break
                raise ContextOverflowError(
                    "a single context frame exceeds the summary input budget"
                )
            current.extend(frame)
            ascii_characters += added_ascii
            non_ascii_characters += added_non_ascii
            has_serialized_messages = has_serialized_messages or bool(serialized_frame)
            consumed_frames += 1
        return current, consumed_frames

    def _summary_context(
        self,
        adapter: RuntimeAdapter,
        agent_system_prompt: str,
        previous_summary: str | None,
        messages: Sequence[MessageRecord],
    ) -> ContextView:
        background = (
            [
                ModelMessage.text(
                    role="user",
                    text=agent_system_prompt,
                    semantic_tag="agent-system-context",
                )
            ]
            if agent_system_prompt
            else []
        )
        return ContextBuilder().build(
            system_prompt=self.prompt,
            messages=[
                *background,
                ModelMessage.text(
                    role="user",
                    text=_summary_payload(previous_summary, messages),
                    semantic_tag=_SUMMARY_INPUT_TAG,
                ),
            ],
            tools=[],
            budget=adapter.model.context_budget,
        )

    async def _summarize_chunk(
        self,
        *,
        adapter: RuntimeAdapter,
        route: str,
        chunk_index: int,
        attempt_index: int,
        input_frame_count: int,
        session_id: str,
        run_id: str | None,
        agent_system_prompt: str,
        previous_summary: str | None,
        messages: Sequence[MessageRecord],
        signal: AbortSignal | None,
        request_recorder: SummaryRequestRecorder | None,
    ) -> _SummaryChunkResult:
        context = self._summary_context(adapter, agent_system_prompt, previous_summary, messages)
        if context.metrics.over_budget:
            raise ContextOverflowError("summary input exceeds model input budget")
        request_id = new_id("req")
        classified_provider_error = False
        usage_failed = False
        provider_done_seen = False
        if request_recorder is not None:
            try:
                await run_blocking(
                    request_recorder.start, request_id, model=adapter.model, metrics=context.metrics
                )
            except Exception as exc:
                raise CompactionStrategyError(
                    f"cannot commit summary dispatch: {exc}", terminal=True
                ) from exc

        async def settle_provider_done(event: ProviderStreamEvent) -> None:
            nonlocal usage_failed, provider_done_seen
            provider_done_seen = True
            if request_recorder is not None:
                try:
                    await run_blocking(
                        request_recorder.settle,
                        request_id,
                        model=adapter.model,
                        provider_done=True,
                        usage=event.usage,
                    )
                except Exception:
                    usage_failed = True
                    raise

        async def observe(progress: ModelExecutionEvent) -> None:
            nonlocal classified_provider_error
            event = progress.event
            if event.type == "error":
                # Classified access failures cannot silently switch billing sources.
                classified_provider_error = event.error_code is not None
            if event.type == "tool_call_delta":
                raise CompactionError("summary provider attempted to call a tool")

        result = await execute_model_request(
            adapter,
            context,
            signal=signal if signal is not None else AbortSignal(),
            request_context=ProviderRequestContext(
                {
                    "session.id": session_id,
                    "request.purpose": "compaction",
                    **({"run.id": run_id} if run_id is not None else {}),
                }
            ),
            on_event=observe,
            on_provider_done=settle_provider_done,
        )
        if not provider_done_seen and request_recorder is not None:
            await run_blocking(
                request_recorder.settle,
                request_id,
                model=adapter.model,
                provider_done=False,
                usage=None,
            )
        error: CompactionError | None = None
        match = _SUMMARY_PATTERN.search(result.text)
        if result.aborted:
            error = CompactionAbortedError("summary compaction was aborted")
        elif result.error is not None:
            error = CompactionStrategyError(
                result.error, terminal=classified_provider_error or usage_failed
            )
        elif match is None or not match.group(1).strip():
            error = CompactionError("summary response did not contain a non-empty <summary> block")

        completion = result.completion or ProviderCompletion()
        usage = _merge_usage({}, result.usage) if result.usage is not None else {}
        output = CompactionModelOutput(
            request_id=request_id,
            route=route,
            chunk_index=chunk_index,
            attempt_index=attempt_index,
            status="failed" if error is not None else "completed",
            requested_output_tokens=adapter.model.max_output_tokens,
            estimated_input_tokens=context.metrics.estimated_input_tokens,
            input_message_count=len(messages),
            input_frame_count=input_frame_count,
            finish_reason=completion.finish_reason,
            provider_finish_reason=completion.provider_finish_reason,
            raw_text=result.text,
            raw_reasoning=result.reasoning,
            model_provider=adapter.model.provider_id,
            model_endpoint_id=adapter.model.endpoint_id,
            model_id=adapter.model.model_id,
            usage=usage,
            error_type=type(error).__name__ if error is not None else None,
            error=str(error) if error is not None else None,
        )
        if error is not None:
            raise CompactionStrategyError(
                str(error),
                model_outputs=(output,),
                terminal=isinstance(error, CompactionStrategyError) and error.terminal,
            ) from error
        assert match is not None
        return _SummaryChunkResult(summary=match.group(1).strip(), usage=usage, model_output=output)


def _apply_compaction_model_config(
    adapter: RuntimeAdapter,
    config: CompactionModelConfig,
) -> RuntimeAdapter:
    """Bind independent summary parameters to any selected route."""

    updates: dict[str, object] = {"max_output_tokens": config.max_output_tokens}
    if config.temperature is not None:
        updates["temperature"] = config.temperature
    if config.reasoning is not None:
        updates["reasoning_request"] = config.reasoning

    try:
        model = type(adapter.model).model_validate(
            adapter.model.model_copy(update=updates).model_dump()
        )
    except ValueError as exc:
        raise CompactionError(f"invalid compaction model override: {exc}") from exc
    return RuntimeAdapter(provider=adapter.provider, model=model)


def _summary_markup_state(raw_text: str) -> tuple[bool, bool]:
    opening = raw_text.find("<summary>")
    if opening < 0:
        return False, False
    return True, raw_text.find("</summary>", opening + len("<summary>")) >= 0


def _should_retry_smaller_chunk(
    error: CompactionStrategyError,
    input_frame_count: int,
) -> bool:
    if input_frame_count <= 1 or len(error.model_outputs) != 1:
        return False
    output = error.model_outputs[0]
    opened, closed = _summary_markup_state(output.raw_text)
    return output.finish_reason == "length" and opened and not closed and not error.terminal


def _outputs_from_error(exc: Exception) -> tuple[CompactionModelOutput, ...]:
    if isinstance(exc, CompactionStrategyError):
        return exc.model_outputs
    return ()


def _serialize_messages(messages: Sequence[MessageRecord]) -> str:
    parts: list[str] = []
    for message in messages:
        label = {
            "system": "System",
            "user": "User",
            "assistant": "Assistant",
            "tool_result": "Tool result",
        }[message.role]
        if message.semantic_tag is not None:
            label += f" ({message.semantic_tag})"
        if message.metadata.get("incomplete"):
            label += " (interrupted/incomplete)"
        parts.append(f"[{label}] {model_visible_message(message).text_content}")
        if message.tool_calls:
            serialized_calls = [call.model_dump(mode="json") for call in message.tool_calls]
            parts.append(f"[Assistant tool calls] {serialized_calls}")
        if message.role == "tool_result" and message.tool_result is not None:
            parts.append(f"[Tool result metadata] {message.tool_result.model_dump(mode='json')}")
    return "\n".join(parts)


def _summary_payload(
    previous_summary: str | None,
    messages: Sequence[MessageRecord],
) -> str:
    sections: list[str] = []
    if previous_summary:
        sections.extend(["<previous_summary>", previous_summary, "</previous_summary>", ""])
    sections.extend(["<conversation>", _serialize_messages(messages), "</conversation>"])
    return "\n".join(sections)


def _merge_usage(
    current: dict[str, int | float],
    usage: Usage | dict[str, int | float],
) -> dict[str, int | float]:
    values = usage.model_dump() if isinstance(usage, Usage) else usage
    merged = dict(current)
    for key, value in values.items():
        if value is None:
            continue
        existing = merged.get(key, 0)
        merged[key] = existing + value if isinstance(existing, int | float) else value
    return merged
