from copy import deepcopy

SCOPE = ("test-service", "test-account")


def message(text="hello", *, item_id="msg", **extra):
    return {
        "type": "message",
        "id": item_id,
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
        **extra,
    }


def reasoning(text="checking", **extra):
    return {
        "type": "reasoning",
        "id": "rs",
        "summary": [{"type": "summary_text", "text": text}],
        "encrypted_content": "opaque-ciphertext",
        **extra,
    }


def call(number=1, **extra):
    return {
        "type": "function_call",
        "id": f"item-{number}",
        "call_id": f"call-{number}",
        "name": "echo",
        "arguments": '{"round":' + str(number) + "}",
        "status": "completed",
        **extra,
    }


def item_event(index, item, *, complete=True):
    return {
        "type": "response.output_item.done" if complete else "response.output_item.added",
        "output_index": index,
        "item": deepcopy(item),
    }


def terminal(items, **extra):
    return {
        "type": "response.completed",
        "response": {
            "status": "completed",
            "output": deepcopy(items),
            "usage": {
                "input_tokens": 20,
                "output_tokens": 10,
                "total_tokens": 30,
                "input_tokens_details": {"cached_tokens": 5},
                "output_tokens_details": {"reasoning_tokens": 4},
            },
            **extra,
        },
    }


def delta(kind, index, item_id, text, **extra):
    return {"type": kind, "output_index": index, "item_id": item_id, "delta": text, **extra}
