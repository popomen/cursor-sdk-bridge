"""Synthetic legacy/new Responses comparison; no requests without explicit --live.

Run against temporary loopback instances. Receipts contain measurements only:
never response text, thinking, prompts, tool data, credentials or raw exceptions.
"""
import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from cursor_sdk_bridge.responses_protocol import prepare_request


SEED = "cb26"
MODEL_PREFIX = "claude-opus-5-5-"
INSTRUCTIONS = ("This is a synthetic adapter acceptance check. Keep the supplied history as context. "
                "For each user turn, reply with exactly its requested check code, with no additional text. "
                "No tools, explanations, data summaries or calculations are needed.")
CONTENT_EVENTS = {
    "response.output_text.delta": "text",
    "response.reasoning_text.delta": "thinking",
    "response.reasoning_summary_text.delta": "thinking",
}


class ProbeProtocolError(RuntimeError):
    """A fixed public error type whose message never contains an upstream payload."""


def endpoint(value):
    """Accept local URLs only; credentials/query data cannot enter receipt metadata."""
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("comparison endpoints must be credential-free loopback HTTP URLs")
    # Force port validation before opening a connection.
    _ = parsed.port
    path = parsed.path.rstrip("/")
    if path in ("", "/v1"):
        path = "/v1/responses"
    if path != "/v1/responses":
        raise ValueError("comparison endpoints must address /v1/responses")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def synthetic_context(rows):
    if rows < 1:
        raise ValueError("rows must be positive")
    return "\n".join(
        f"seed={SEED} row={index:06d} digest={hashlib.sha256(f'{SEED}:{index}'.encode()).hexdigest()[:12]} "
        "summary=stable synthetic context for reuse checks."
        for index in range(rows))


def check_code(run_id, turn):
    return f"CB_CHECK_{run_id[:8]}_T{turn:02d}"


def user_turn(run_id, turn):
    return {"role": "user", "content": f"Reply with exactly {check_code(run_id, turn)}"}


def request_body(history, model):
    return {"model": MODEL_PREFIX + model, "instructions": INSTRUCTIONS, "input": history,
            "tools": [], "tool_choice": "none", "stream": True, "store": False}


def estimate(rows=1200, turns=3, model="high"):
    if turns < 1:
        raise ValueError("turns must be positive")
    body = request_body([{"role": "user", "content": synthetic_context(rows)}, user_turn("estimate", 1)], model)
    _, prompt, _ = prepare_request(body)
    size = len(prompt.encode())
    low, high = round(size / 5), round(size / 3)
    return {"live": False, "model": MODEL_PREFIX + model, "rows": rows, "turns_per_mode": turns,
            "sdk_requests_max": 2 * turns, "first_turn_prompt_bytes": size,
            "estimated_first_turn_input_tokens": {"min": low, "max": high},
            "estimated_total_context_input_tokens": {"min": 2 * turns * low, "max": 2 * turns * high},
            "estimate_basis": "UTF-8 prompt bytes / 5 to bytes / 3; approximate, not a tokenizer or billing quote",
            "quota_note": "Two long initial turns plus follow-ups; cache billing and hidden reasoning vary. "
                          "Output asks for one short check code per request. --live explicitly starts inference."}


def _sse_events(response):
    event, data, frame_size = None, [], 0
    while True:
        raw = response.readline(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ProbeProtocolError("SSE line exceeds the probe bound")
        if not raw:
            if data:
                raise ProbeProtocolError("SSE ended inside a frame")
            return
        frame_size += len(raw)
        if frame_size > 2 * 1024 * 1024:
            raise ProbeProtocolError("SSE frame exceeds the probe bound")
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                text = "\n".join(data)
                if text == "[DONE]":
                    return
                value = json.loads(text)
                if not isinstance(value, dict):
                    raise ProbeProtocolError("SSE data must be an object")
                yield event or value.get("type", ""), value
            event, data, frame_size = None, [], 0
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)


def _nonnegative_integer(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _usage(value):
    value = value if isinstance(value, dict) else {}
    details = value.get("input_tokens_details")
    details = details if isinstance(details, dict) else {}
    return {"input_tokens": _nonnegative_integer(value.get("input_tokens")),
            "output_tokens": _nonnegative_integer(value.get("output_tokens")),
            "cache_read_tokens": _nonnegative_integer(details.get("cached_tokens")),
            "cache_write_tokens": _nonnegative_integer(details.get("cache_write_tokens"))}


@dataclass
class TurnResult:
    metadata: dict
    output: list


def stream_turn(url, body, expected_code, *, opener=None, clock=time.monotonic, timeout=1235):
    """Measure real SSE arrival times. Only the caller's transient output retains text."""
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    raw = json.dumps(body, ensure_ascii=False).encode()
    _, prompt, _ = prepare_request(body)
    request = urllib.request.Request(endpoint(url), data=raw,
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
    began = clock()
    report = {"completed": False, "output_matches": False, "event_count": 0,
              "request_bytes": len(raw), "prompt_bytes": len(prompt.encode()),
              "first_content_s": None, "first_text_s": None, "first_thinking_s": None,
              "completion_s": None, "duration_s": None, "streamed_before_completion": False,
              "usage": _usage(None), "error_type": None}
    output = []
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.headers.get_content_type() != "text/event-stream":
                raise ProbeProtocolError("Expected Responses SSE")
            for kind, value in _sse_events(response):
                now = round(clock() - began, 6)
                report["event_count"] += 1
                content_kind = CONTENT_EVENTS.get(kind)
                if content_kind and isinstance(value.get("delta"), str) and value["delta"]:
                    if report["first_content_s"] is None:
                        report["first_content_s"] = now
                    field = "first_" + content_kind + "_s"
                    if report[field] is None:
                        report[field] = now
                if kind in ("response.failed", "response.incomplete", "error"):
                    report["error_type"] = "upstream_" + kind.rsplit(".", 1)[-1]
                    break
                if kind == "response.completed":
                    result = value.get("response", {})
                    if not isinstance(result, dict) or not isinstance(result.get("output"), list):
                        raise ProbeProtocolError("Invalid completed response")
                    output = result["output"]
                    text = "".join(part.get("text", "") for item in output if item.get("type") == "message"
                                   for part in item.get("content", []) if part.get("type") == "output_text")
                    report.update(completed=result.get("status") == "completed", completion_s=now,
                                  output_matches=text.strip() == expected_code, usage=_usage(result.get("usage")))
                    break
        if not report["completed"] and report["error_type"] is None:
            report["error_type"] = "missing_completed_event"
    except Exception as error:
        # Exception messages can include entire HTTP payloads, URLs or output.
        report["error_type"] = type(error).__name__
        if isinstance(error, urllib.error.HTTPError):
            report["http_status"] = error.code
            error.close()
    finally:
        report["duration_s"] = round(clock() - began, 6)
    if report["completed"] and report["first_content_s"] is not None:
        # A nontrivial lead rules out buffered deltas emitted just before completion.
        report["streamed_before_completion"] = report["completion_s"] - report["first_content_s"] >= 0.1
    return TurnResult(report, output)


def summarize(turns, expected_turns):
    grouped = {mode: [turn for turn in turns if turn["mode"] == mode] for mode in ("legacy", "new")}
    valid = all(len(values) == expected_turns and all(turn["completed"] and turn["output_matches"] for turn in values)
                for values in grouped.values())
    medians = {}
    for mode, values in grouped.items():
        followups = [turn for turn in values if turn["turn"] > 1]
        cache = [turn["usage"]["cache_read_tokens"] for turn in followups]
        durations = [turn["duration_s"] for turn in followups]
        medians[mode] = {"followup_cache_read_tokens": statistics.median(cache) if cache and None not in cache else None,
                         "followup_duration_s": statistics.median(durations) if durations else None}
    old_cache, new_cache = (medians[mode]["followup_cache_read_tokens"] for mode in ("legacy", "new"))
    old_time, new_time = (medians[mode]["followup_duration_s"] for mode in ("legacy", "new"))
    enough = valid and expected_turns >= 2 and None not in (old_cache, new_cache, old_time, new_time)
    cache_improved = bool(enough and new_cache >= max(old_cache * 2, old_cache + 1000))
    time_improved = bool(enough and new_time <= old_time * 0.9)
    streamed = valid and any(turn["streamed_before_completion"] for turn in grouped["new"])
    return {"outcome": "demonstrated" if cache_improved and time_improved and streamed else "not_demonstrated",
            "all_turns_completed_and_matched": valid, "followup_medians": medians,
            "cache_improvement_demonstrated": cache_improved, "duration_improvement_demonstrated": time_improved,
            "streaming_demonstrated": streamed,
            "thresholds": {"cache_min_ratio": 2, "cache_min_gain_tokens": 1000,
                           "duration_max_ratio": 0.9, "stream_content_lead_min_s": 0.1},
            "limitation": "One synthetic comparison is descriptive, not statistical proof; cache warmup, "
                          "network and reasoning variance may affect results. No improvement is assumed."}


def compare(legacy_url, new_url, *, rows=1200, turns=3, model="high", runner=stream_turn, run_id=None):
    if turns < 1:
        raise ValueError("turns must be positive")
    urls = {"legacy": endpoint(legacy_url), "new": endpoint(new_url)}
    if urls["legacy"] == urls["new"]:
        raise ValueError("legacy and new endpoints must differ")
    run_id = run_id or uuid.uuid4().hex
    context = synthetic_context(rows)
    initial = {"role": "user", "content": f"Synthetic comparison run {run_id}.\n{context}"}
    histories = {mode: [dict(initial)] for mode in urls}
    active = set(urls)
    measurements = []
    for turn in range(1, turns + 1):
        # Alternate which endpoint starts each pair to disclose/reduce order bias.
        order = ("legacy", "new") if turn % 2 else ("new", "legacy")
        for mode in order:
            if mode not in active:
                continue
            histories[mode].append(user_turn(run_id, turn))
            result = runner(urls[mode], request_body(histories[mode], model), check_code(run_id, turn))
            measurements.append({"mode": mode, "turn": turn, **result.metadata})
            if result.metadata["completed"] and result.metadata["output_matches"]:
                histories[mode].extend(result.output)
            else:
                active.remove(mode)
    return {"schema": 1, "created_at": datetime.now(timezone.utc).isoformat(), "live": True,
            "run_id": run_id, "model": MODEL_PREFIX + model, "rows": rows, "turns_per_mode": turns,
            "corpus_sha256": hashlib.sha256(context.encode()).hexdigest(), "endpoints": urls,
            "method": {"order": "interleaved, alternating first endpoint by turn", "previous_response_id": False,
                       "same_seed_and_initial_history": True, "dedup_avoidance": "fresh shared run ID in initial input",
                       "prompt_bytes_basis": "full prepare_request prompt, not the SDK's smaller resumed send",
                       "retained_data": "metadata only; no model output, thinking, input text or raw errors"},
            "estimate": estimate(rows, turns, model), "turns": measurements,
            "summary": summarize(measurements, turns)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-url", required=True)
    parser.add_argument("--new-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rows", type=int, default=1200)
    parser.add_argument("--turns", type=int, default=3)
    parser.add_argument("--model", choices=("high", "xhigh", "max"), default="high")
    parser.add_argument("--live", action="store_true", help="explicitly consume Cursor quota for real comparisons")
    args = parser.parse_args(argv)
    try:
        legacy, new = endpoint(args.legacy_url), endpoint(args.new_url)
        if legacy == new:
            raise ValueError("legacy and new endpoints must differ")
        cost = estimate(args.rows, args.turns, args.model)
        print(json.dumps(cost, ensure_ascii=False), flush=True)
        if not args.live:
            return 0
        receipt = compare(legacy, new, rows=args.rows, turns=args.turns, model=args.model)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"output": str(args.output), "summary": receipt["summary"]}, ensure_ascii=False))
        return 0 if receipt["summary"]["outcome"] == "demonstrated" else 1
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "outcome": "not_demonstrated"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
