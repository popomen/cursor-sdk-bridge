"""Strict JSON and host-tool output validation shared by the API adapters."""
import json


def strict_json(text):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON property")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("Non-finite JSON number")

    return json.loads(text, object_pairs_hook=object_pairs, parse_constant=reject_constant)


def parse_output(text, tools):
    from jsonschema import Draft202012Validator

    value = strict_json(text)
    if not isinstance(value, dict) or set(value) != {"output"}:
        raise ValueError("Expected one output array")
    if not isinstance(value["output"], list) or not value["output"]:
        raise ValueError("Empty or invalid output array")
    schemas = {tool["name"]: tool["parameters"] for tool in tools}
    calls = set()
    normalized = []
    for item in value["output"]:
        if not isinstance(item, dict):
            raise ValueError("Output item must be an object")
        if item.get("type") == "message":
            if set(item) != {"type", "text"} or not isinstance(item["text"], str):
                raise ValueError("Invalid message")
            normalized.append(item)
        elif item.get("type") == "function_call":
            if set(item) != {"type", "call_id", "name", "arguments"}:
                raise ValueError("Invalid function_call fields")
            call_id = item["call_id"]
            if not isinstance(call_id, str) or not call_id or call_id in calls:
                raise ValueError("Missing or duplicate call_id")
            calls.add(call_id)
            if not isinstance(item["name"], str) or item["name"] not in schemas:
                raise ValueError("Unknown tool")
            if not isinstance(item["arguments"], str):
                raise ValueError("arguments must be a JSON string")
            arguments = strict_json(item["arguments"])
            Draft202012Validator(schemas[item["name"]]).validate(arguments)
            normalized.append({"type": "function_call", "name": item["name"], "arguments": arguments})
        else:
            raise ValueError("Unknown output type")
    return value["output"], normalized
