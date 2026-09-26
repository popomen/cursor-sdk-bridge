"""Anthropic Messages translation onto the strict Responses core. This module never executes tools."""
import json
import uuid

from cursor_bridge.failures import PromptTooLarge
from cursor_bridge.responses_protocol import InvalidRequest, MODELS, prepare_request

HOST = "Claude Code Messages"
# Adapter prompts average about 2.3 bytes per SDK token, plus a ~3k-token SDK preamble.
SDK_OVERHEAD_TOKENS = 3000
IMAGE_TOKENS = 1600
OMITTED = "[{} block omitted: the Cursor adapter does not support it]"
ERROR_TYPES = {400: "invalid_request_error", 404: "not_found_error", 413: "request_too_large",
               429: "rate_limit_error"}
ZERO_USAGE = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}


def system_text(system):
    if system is None or isinstance(system, str):
        return system or ""
    if isinstance(system, list) and all(isinstance(block, dict) and block.get("type") == "text"
                                        and isinstance(block.get("text"), str) for block in system):
        return "\n\n".join(block["text"] for block in system)
    raise InvalidRequest("system must be text or an array of text blocks", "system")


def content_part(block, text_type):
    """Translate a text or base64 image block; any other block becomes a visible placeholder."""
    kind = block.get("type") if isinstance(block, dict) else None
    if kind == "text" and isinstance(block.get("text"), str):
        return {"type": text_type, "text": block["text"]}
    source = block.get("source") if kind == "image" else None
    if (isinstance(source, dict) and source.get("type") == "base64"
            and isinstance(source.get("media_type"), str) and isinstance(source.get("data"), str)):
        return {"type": "input_image", "image_url": "data:" + source["media_type"] + ";base64," + source["data"]}
    return {"type": text_type, "text": OMITTED.format(kind if isinstance(kind, str) else "unknown")}


def call_item(block, param):
    if not (isinstance(block.get("id"), str) and isinstance(block.get("name"), str)
            and isinstance(block.get("input"), dict)):
        raise InvalidRequest("tool_use needs an id, a name and an input object", param)
    return {"type": "function_call", "call_id": block["id"], "name": block["name"],
            "arguments": json.dumps(block["input"], ensure_ascii=False)}


def result_item(block, param):
    content, failed = block.get("content") or "", bool(block.get("is_error"))
    if isinstance(content, str) and not failed:
        output = content
    elif isinstance(content, (str, list)):
        parts = [{"type": "text", "text": content}] if isinstance(content, str) else content
        output = [{"type": "input_text", "text": "[tool_result is_error=true]"}] if failed else []
        output += [content_part(part, "input_text") for part in parts]
    else:
        raise InvalidRequest("tool_result content must be text or an array", param + ".content")
    return {"type": "function_call_output", "call_id": block.get("tool_use_id"), "output": output}


def convert_messages(messages):
    if not isinstance(messages, list) or not messages:
        raise InvalidRequest("messages must be a nonempty array", "messages")
    items = []
    for index, message in enumerate(messages):
        param = f"messages[{index}]"
        role = message.get("role") if isinstance(message, dict) else None
        if role not in ("user", "assistant", "system"):
            raise InvalidRequest("message role must be user, assistant or system", param + ".role")
        content = message.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list):
            raise InvalidRequest("message content must be text or an array", param + ".content")
        text_type = "output_text" if role == "assistant" else "input_text"
        parts = []
        for block_index, block in enumerate(content):
            kind = block.get("type") if isinstance(block, dict) else None
            if kind in ("thinking", "redacted_thinking"):
                continue
            if (kind, role) not in (("tool_use", "assistant"), ("tool_result", "user")):
                parts.append(content_part(block, text_type))
                continue
            if parts:
                items.append({"role": role, "content": parts})
                parts = []
            path = f"{param}.content[{block_index}]"
            items.append(call_item(block, path) if kind == "tool_use" else result_item(block, path))
        if parts:
            items.append({"role": role, "content": parts})
    return items


def convert_tools(tools):
    if tools is None:
        return []
    if not isinstance(tools, list):
        raise InvalidRequest("tools must be an array", "tools")
    converted = []
    for index, tool in enumerate(tools):
        if not isinstance(tool, dict):
            raise InvalidRequest("tools must be objects", f"tools[{index}]")
        # Server tools such as web search run on Anthropic's servers; the SDK cannot execute them.
        if tool.get("type") in (None, "custom"):
            converted.append({"type": "function", "name": tool.get("name"), "description": tool.get("description", ""),
                              "parameters": tool.get("input_schema")})
    return converted


def convert_choice(choice):
    """Return the Responses tool_choice and parallel_tool_calls for an Anthropic tool_choice."""
    if choice is None:
        return "auto", True
    kind = choice.get("type") if isinstance(choice, dict) else None
    if kind not in ("auto", "any", "tool", "none"):
        raise InvalidRequest("tool_choice type must be auto, any, tool or none", "tool_choice.type")
    mapped = {"type": "function", "name": choice.get("name")} if kind == "tool" else {"any": "required"}.get(kind, kind)
    return mapped, not choice.get("disable_parallel_tool_use", False)


def prepare_messages(body):
    """Return an equivalent Responses body with its history, prompt and images for the shared SDK path."""
    if not isinstance(body, dict) or body.get("model") not in MODELS:
        raise InvalidRequest("model must be an Opus 5.5 high, xhigh or max alias", "model")
    if not isinstance(body.get("stream", False), bool):
        raise InvalidRequest("stream must be boolean", "stream")
    choice, parallel = convert_choice(body.get("tool_choice"))
    request = {"model": body["model"], "instructions": system_text(body.get("system")),
               "input": convert_messages(body.get("messages")), "tools": convert_tools(body.get("tools")),
               "tool_choice": choice, "parallel_tool_calls": parallel, "store": False}
    history, prompt, images = prepare_request(request, host=HOST)
    return request, history, prompt, images


def message_shell(model):
    return {"id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant", "model": model, "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": dict(ZERO_USAGE)}


def usage_block(usage):
    """SDK input tokens include cache reads and writes; Anthropic reports the three separately."""
    if not usage:
        return dict(ZERO_USAGE)
    read, write = usage["cache_read_tokens"], usage["cache_write_tokens"]
    return {"input_tokens": max(0, usage["input_tokens"] - read - write), "output_tokens": usage["output_tokens"],
            "cache_creation_input_tokens": write, "cache_read_input_tokens": read}


def complete_message(shell, response, usage=None):
    content = []
    for item in response["output"]:
        if item["type"] == "message":
            content.append({"type": "text", "text": item["content"][0]["text"]})
        else:
            content.append({"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:24], "name": item["name"],
                            "input": json.loads(item["arguments"])})
    calls = any(block["type"] == "tool_use" for block in content)
    return {**shell, "content": content, "stop_reason": "tool_use" if calls else "end_turn", "usage": usage_block(usage)}


def stream_events(message, chunk=256):
    """Yield the events after message_start; deltas contain only validated output."""
    for index, block in enumerate(message["content"]):
        if block["type"] == "text":
            start, delta, field, value = {"type": "text", "text": ""}, "text_delta", "text", block["text"]
        else:
            start, delta, field = {**block, "input": {}}, "input_json_delta", "partial_json"
            value = json.dumps(block["input"], ensure_ascii=False)
        yield "content_block_start", {"type": "content_block_start", "index": index, "content_block": start}
        for offset in range(0, len(value), chunk):
            yield "content_block_delta", {"type": "content_block_delta", "index": index,
                                          "delta": {"type": delta, field: value[offset:offset + chunk]}}
        yield "content_block_stop", {"type": "content_block_stop", "index": index}
    yield "message_delta", {"type": "message_delta", "usage": message["usage"],
                            "delta": {"stop_reason": message["stop_reason"], "stop_sequence": None}}
    yield "message_stop", {"type": "message_stop"}


def estimate_tokens(prompt_bytes, images=()):
    """Rough count_tokens answer; inference usage comes from the SDK."""
    return SDK_OVERHEAD_TOKENS + -(-prompt_bytes * 10 // 23) + IMAGE_TOKENS * len(images)


def prompt_too_long(prompt_bytes, limit):
    return f"prompt is too long: {estimate_tokens(prompt_bytes)} tokens > {estimate_tokens(limit)} maximum"


def error_body(kind, message):
    return {"type": "error", "error": {"type": kind, "message": message}}


def failure_error(label, prompt_bytes, limit):
    """HTTP status and Anthropic error body for an adapter failure label; never upstream text."""
    if label == PromptTooLarge.label:
        return 400, error_body("invalid_request_error", prompt_too_long(prompt_bytes, limit))
    return 500, error_body("api_error", "Cursor SDK response failed (" + label + ")")
