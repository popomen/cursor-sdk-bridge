"""Replayable in-memory deltas shared by an inference and its HTTP subscribers."""
import copy
import threading
import uuid

from cursor_bridge.failures import UpstreamIncomplete


class LiveOutput:
    def __init__(self):
        self.lock = threading.Lock()
        self.events, self.items = [], []
        self.bytes = 0

    def append(self, event):
        if not isinstance(event, dict):
            return  # Legacy/reuse callbacks may expose raw SDK dataclasses.
        kind, text = event.get("type"), event.get("text")
        if kind not in ("text_delta", "thinking_delta") or not isinstance(text, str) or not text:
            return
        with self.lock:
            self.bytes += len(text.encode())
            if self.bytes > 16 * 1024 * 1024:
                raise UpstreamIncomplete()
            item_kind = "message" if kind == "text_delta" else "reasoning"
            if not self.items or self.items[-1]["type"] != item_kind:
                item = {"id": ("msg_" if item_kind == "message" else "rs_") + uuid.uuid4().hex,
                        "type": item_kind, "status": "in_progress"}
                if item_kind == "message":
                    item.update(role="assistant", content=[{"type": "output_text", "text": "", "annotations": [], "logprobs": []}])
                else:
                    item["summary"] = [{"type": "summary_text", "text": ""}]
                self.items.append(item)
            item = self.items[-1]
            part = item["content"][0] if item_kind == "message" else item["summary"][0]
            part["text"] += text
            self.events.append({"index": len(self.items) - 1, "id": item["id"], "type": item_kind, "text": text})

    def read(self, cursor):
        with self.lock:
            return len(self.events), self.events[cursor:]

    def finalize(self, response):
        """Keep streamed IDs/content stable in the terminal response and retry store."""
        with self.lock:
            streamed = "".join(item["content"][0]["text"] for item in self.items if item["type"] == "message")
        full = "".join(item["content"][0]["text"] for item in response["output"] if item["type"] == "message")
        if not full.startswith(streamed):
            raise UpstreamIncomplete()
        if full[len(streamed):]:
            self.append({"type": "text_delta", "text": full[len(streamed):]})
        with self.lock:
            if not self.items:
                return response
            output = copy.deepcopy(self.items)
        for item in output:
            item["status"] = "completed"
        output += [item for item in response["output"] if item["type"] == "function_call"]
        return {**response, "output": output}


class ResponsesLive:
    def __init__(self, emit):
        self.emit, self.current, self.text = emit, None, ""

    def close_item(self):
        if self.current is None:
            return
        event, text = self.current, self.text
        common = {"item_id": event["id"], "output_index": event["index"]}
        if event["type"] == "message":
            common["content_index"] = 0
            part = {"type": "output_text", "text": text, "annotations": [], "logprobs": []}
            self.emit("response.output_text.done", {**common, "text": text})
            self.emit("response.content_part.done", {**common, "part": part})
            item = {"id": event["id"], "type": "message", "role": "assistant", "status": "completed", "content": [part]}
        else:
            common["summary_index"] = 0
            part = {"type": "summary_text", "text": text}
            self.emit("response.reasoning_summary_text.done", {**common, "text": text})
            self.emit("response.reasoning_summary_part.done", {**common, "part": part})
            item = {"id": event["id"], "type": "reasoning", "status": "completed", "summary": [part]}
        self.emit("response.output_item.done", {"output_index": event["index"], "item": item})
        self.current, self.text = None, ""

    def delta(self, event):
        if self.current is None or self.current["id"] != event["id"]:
            self.close_item()
            self.current = event
            common = {"item_id": event["id"], "output_index": event["index"]}
            item = {"id": event["id"], "type": event["type"], "status": "in_progress"}
            if event["type"] == "message":
                item.update(role="assistant", content=[])
            else:
                item["summary"] = []
            self.emit("response.output_item.added", {"output_index": event["index"], "item": item})
            if event["type"] == "message":
                self.emit("response.content_part.added", {**common, "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": [], "logprobs": []}})
            else:
                self.emit("response.reasoning_summary_part.added", {**common, "summary_index": 0,
                    "part": {"type": "summary_text", "text": ""}})
        self.text += event["text"]
        common = {"item_id": event["id"], "output_index": event["index"], "delta": event["text"]}
        if event["type"] == "message":
            self.emit("response.output_text.delta", {**common, "content_index": 0, "logprobs": []})
        else:
            self.emit("response.reasoning_summary_text.delta", {**common, "summary_index": 0})


class MessagesLive:
    def __init__(self, emit):
        self.emit, self.current = emit, None

    def close_item(self):
        if self.current is not None:
            if self.current["type"] == "reasoning":
                self.emit("content_block_delta", {"type": "content_block_delta", "index": self.current["index"],
                    "delta": {"type": "signature_delta", "signature": ""}})
            self.emit("content_block_stop", {"type": "content_block_stop", "index": self.current["index"]})
            self.current = None

    def delta(self, event):
        if self.current is None or self.current["id"] != event["id"]:
            self.close_item()
            self.current = event
            block = {"type": "text", "text": ""} if event["type"] == "message" else {"type": "thinking", "thinking": "", "signature": ""}
            self.emit("content_block_start", {"type": "content_block_start", "index": event["index"], "content_block": block})
        delta = {"type": "text_delta", "text": event["text"]} if event["type"] == "message" else {
            "type": "thinking_delta", "thinking": event["text"]}
        self.emit("content_block_delta", {"type": "content_block_delta", "index": event["index"], "delta": delta})
