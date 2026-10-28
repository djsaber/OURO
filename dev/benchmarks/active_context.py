"""Measure full active input preparation and codec JSON encoding, without network or IO.

uv run --frozen python -m dev.benchmarks.active_context --repeat 3
"""

from __future__ import annotations

import argparse
import base64
import gc
import json
import platform
import tracemalloc
from time import perf_counter

from ouro.core.context import ContextBuilder
from ouro.core.messages import ImageContent, MessageRecord
from ouro.providers.anthropic_messages.codec import messages_to_anthropic_messages
from ouro.providers.openai_chat_completions.codec import messages_to_chat_completions
from ouro.providers.openai_responses.codec import responses_input


def encode(view, codec):
    context = view.provider_context
    common = {
        "system_prompt": context.system_prompt,
        "provider_id": "benchmark",
        "model_id": "benchmark",
        "supports_image_input": True,
    }
    if codec == "chat":
        value = messages_to_chat_completions(context.messages, **common)
    elif codec == "responses":
        value = responses_input(context.messages, scope=(), **common)
    else:
        value = messages_to_anthropic_messages(context.messages, **common)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def experiment(count, media_bytes, repeat):
    messages = [
        MessageRecord.text(
            session_id="synthetic",
            role="user" if index % 2 == 0 else "assistant",
            text=f"message {index}: " + "x" * (40 * 1024),
        )
        for index in range(count)
    ]
    if media_bytes:
        messages[0].content.append(
            ImageContent(
                media_type="image/jpeg",
                uri="data:image/jpeg;base64," + base64.b64encode(b"x" * media_bytes).decode(),
            )
        )

    def build():
        return ContextBuilder().build(messages=messages, source_messages=messages, tools=[])

    for index in range(repeat):
        gc.collect()
        start = perf_counter()
        view = build()
        prepare_seconds = perf_counter() - start
        sizes, times = {}, {}
        for codec in ("chat", "responses", "anthropic"):
            start = perf_counter()
            payload = encode(view, codec)
            times[codec] = perf_counter() - start
            sizes[codec] = len(payload)
            del payload
        # Allocation measurements are separate from the uninstrumented timings.
        del view
        gc.collect()
        tracemalloc.start()
        try:
            view = build()
            preparation_peak = tracemalloc.get_traced_memory()[1]
            peaks = {}
            for codec in ("chat", "responses", "anthropic"):
                current = tracemalloc.get_traced_memory()[0]
                tracemalloc.reset_peak()
                payload = encode(view, codec)
                peaks[codec] = tracemalloc.get_traced_memory()[1] - current
                assert len(payload) == sizes[codec]
                del payload
        finally:
            tracemalloc.stop()
        print(
            json.dumps(
                {
                    "messages": count,
                    "text_bytes_each": 40 * 1024,
                    "media_raw_bytes": media_bytes,
                    "repeat": index,
                    "prepare_seconds": prepare_seconds,
                    "preparation_python_peak_bytes": preparation_peak,
                    "codec_seconds": times,
                    "encoded_bytes": sizes,
                    "encoding_python_peak_bytes": peaks,
                }
            ),
            flush=True,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("repeat must be positive")
    print(
        json.dumps(
            {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "scope": "fresh builder; real codecs plus JSON UTF-8; no storage/network",
                "memory": "separate Python allocation peak; not RSS; input construction excluded",
            }
        ),
        flush=True,
    )
    for count, media_bytes in ((4, 0), (128, 0), (4, 4 * 1024 * 1024)):
        experiment(count, media_bytes, args.repeat)


if __name__ == "__main__":
    main()
