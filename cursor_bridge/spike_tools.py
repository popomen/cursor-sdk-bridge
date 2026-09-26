#!/usr/bin/env python3
"""Stage B experiment only: asks for tool-call JSON, never executes that JSON."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

PROXY_VARS = ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")

MODEL = "claude-opus-5-5-high"
FIXTURES = Path(__file__).with_name("spike_cases.json")
NATIVE_TOOL_EVENTS = {"toolCall", "tool-call-started", "tool-call-completed", "partial-tool-call"}


def model_identity(selection):
    return selection["id"], {p["id"]: p["value"] for p in selection.get("params", [])}


def resolve_model(models):
    """Resolve the requested high alias using the account's actual catalog."""
    if any(model.id == MODEL for model in models):
        return {"id": MODEL}
    base = next((model for model in models if model.id == "claude-opus-5-5"), None)
    if base is None:
        raise ValueError("Requested Opus 5.5 model absent from catalog")
    defaults = [variant for variant in base.variants if variant.is_default]
    if len(defaults) != 1:
        raise ValueError("Catalog must define one default Opus 5.5 variant")
    params = {param.id: param.value for param in defaults[0].params}
    params["effort"] = "high"
    if not any({p.id: p.value for p in v.params} == params for v in base.variants):
        raise ValueError("Requested high variant absent from catalog")
    return {"id": base.id, "params": [{"id": name, "value": value} for name, value in params.items()]}


class DeadlineExpired(RuntimeError):
    # socket.timeout aliases TimeoutError; httpcore would mislabel our SIGALRM
    # exception as an HTTP read timeout if it inherited that class.
    pass


@contextmanager
def deadline(seconds):
    """Linux CLI wall-clock deadline; SDK keepalives cannot extend it."""
    def expired(signum, frame):
        raise DeadlineExpired("SDK operation exceeded its wall-clock deadline")

    old_handler = signal.signal(signal.SIGALRM, expired)
    old_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    started = time.monotonic()
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        if old_timer[0]:
            signal.setitimer(signal.ITIMER_REAL, max(0.001, old_timer[0] - (time.monotonic() - started)), old_timer[1])


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


def build_prompt(tools, request):
    return (
        "You are the model behind a Codex tool adapter. The host executes the tools. "
        "Return exactly one JSON object with an output array, without Markdown. "
        "Each item is either {\"type\":\"function_call\",\"call_id\":\"unique-id\","
        "\"name\":\"tool-name\",\"arguments\":\"JSON-encoded argument object\"} "
        "or {\"type\":\"message\",\"text\":\"answer\"}. "
        "Do not use native agent tools or execute any command yourself. "
        "Use only supplied functions and supplied argument values; omit optional arguments "
        "unless requested. A later function_call_output is the host's result for that call_id.\n"
        "Codex tools schema:\n" + json.dumps(tools, ensure_ascii=False) + "\nRequest:\n" + request
    )


def snapshot(workspace):
    result = {}
    for path in sorted(workspace.rglob("*")):
        name = str(path.relative_to(workspace))
        if path.is_symlink():
            result[name] = ("symlink", os.readlink(path))
        elif path.is_file():
            result[name] = ("file", hashlib.sha256(path.read_bytes()).hexdigest())
        else:
            result[name] = ("directory",)
    return result


def safe_error(exc):
    result = {"error_type": type(exc).__name__, "http_status": getattr(exc, "status_code", None)}
    code = getattr(exc, "code", None)
    if code in {"unauthenticated", "permission_denied", "unavailable", "deadline_exceeded",
                "not_found", "invalid_argument", "internal", "cancelled", "resource_exhausted", "unknown"}:
        result["sdk_error_code"] = code
    causes = []
    cause = exc.__cause__
    while cause is not None and len(causes) < 4:
        causes.append(type(cause).__name__)
        cause = cause.__cause__
    if causes:
        result["cause_types"] = causes
    if hasattr(exc, "spike_diagnostics"):
        result["stream_diagnostics"] = exc.spike_diagnostics
    if hasattr(exc, "spike_operation"):
        result["failed_operation"] = exc.spike_operation
    if hasattr(exc, "spike_cleanup_errors"):
        result["cleanup_errors"] = exc.spike_cleanup_errors
    return result


def wait_with_diagnostics(run):
    """Python 1.0.32 drops RunResult.error; retain safe status classifications."""
    started = time.monotonic()
    stats = {"phase": "stream", "events": {}, "full_result_seen": False, "done_seen": False}
    flags = set()
    try:
        for event in run.events():
            kind = getattr(event, "kind", "unknown")
            if kind not in {"sdk_message", "interaction_update", "step", "result", "done"}:
                kind = "unknown"
            stats["events"][kind] = stats["events"].get(kind, 0) + 1
            stats["last_event_seconds"] = round(time.monotonic() - started, 3)
            stats["full_result_seen"] |= bool(getattr(event, "result_is_full", False))
            stats["done_seen"] |= kind == "done"
            update = getattr(event, "interaction_update", None)
            if getattr(update, "type", None) == "text-delta":
                stats.setdefault("first_text_seconds", stats["last_event_seconds"])
            message = event.sdk_message
            if message is None or getattr(message, "type", None) != "status":
                continue
            status = getattr(message, "status", "").lower()
            if status in {"running", "finished", "error", "cancelled", "expired"}:
                stats["last_status"] = status
            detail = getattr(message, "message", "").lower()
            for name, needle in (
                ("region_unsupported", "not supported in your region"),
                ("model_unavailable", "model not available"),
                ("unauthenticated", "unauthenticated"),
                ("invalid_api_key", "invalid user api key"),
                ("permission_denied", "permission denied"),
                ("timeout", "timed out"),
            ):
                if needle in detail:
                    flags.add(name)
        stats["phase"] = "terminal_wait"
        result = run.wait()
        stats["phase"] = "finished"
    except Exception as exc:
        exc.spike_diagnostics = stats
        raise
    finally:
        stats["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return SimpleNamespace(status=result.status, result=result.result,
                           model=result.model, sdk_error_flags=sorted(flags), stream_diagnostics=stats)


@contextmanager
def managed_client(command, workspace, state):
    from cursor_sdk import CursorClient, DefaultHttpxClient

    with DefaultHttpxClient(trust_env=False, timeout=120) as http:
        client = CursorClient.launch_bridge(
            command=command, workspace=workspace, state_root=state,
            timeout=15, client_timeout=120, max_retries=0,
            http_client=http, allow_api_key_env_fallback=False,
        )
        # Pinned SDK 1.0.32 clears this ownership field before its shutdown RPC.
        process = client._owned_bridge.process
        primary = None
        try:
            yield client
        except BaseException as exc:
            primary = exc
            raise
        finally:
            cleanup_errors = []
            try:
                with deadline(15):
                    client.close()
            except Exception as exc:
                exc.spike_operation = "bridge_close"
                cleanup_errors.append(exc)
            try:
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            except Exception as exc:
                exc.spike_operation = "bridge_process_cleanup"
                cleanup_errors.append(exc)
            if cleanup_errors:
                if primary is not None:
                    primary.spike_cleanup_errors = getattr(primary, "spike_cleanup_errors", []) + [
                        safe_error(exc) for exc in cleanup_errors]
                else:
                    first, *remaining = cleanup_errors
                    if remaining:
                        first.spike_cleanup_errors = [safe_error(exc) for exc in remaining]
                    raise first


def exercise(send, workspace, fixture, tools, key="", expected_model=None):
    """One case, with an optional real protocol follow-up; send is injectable."""
    results = []
    prompt = build_prompt(tools, fixture["request"])
    expected = fixture["expected"]
    for turn in range(2 if fixture.get("followup") else 1):
        before = snapshot(workspace)
        events = Counter()

        def observe(event):
            kind = event.get("type") if isinstance(event, dict) else getattr(event, "type", None)
            if isinstance(kind, str):
                events[kind] += 1

        record = {"case": fixture["name"], "turn": turn + 1, "passed": False}
        text = ""
        parsed = None
        started = time.monotonic()
        try:
            result = send(prompt, observe)
            record["sdk_status"] = result.status
            record["sdk_error_flags"] = getattr(result, "sdk_error_flags", [])
            if hasattr(result, "stream_diagnostics"):
                record["stream_diagnostics"] = result.stream_diagnostics
            text = result.result
            actual_model = getattr(result, "model", None)
            if actual_model is not None:
                record["sdk_model"] = actual_model.to_json()
                if model_identity(record["sdk_model"]) != model_identity(expected_model or {"id": MODEL}):
                    raise ValueError("SDK returned a different model; inspect catalog mapping")
            if result.status != "finished":
                raise ValueError("SDK run did not finish successfully")
            parsed, normalized = parse_output(text, tools)
            record["parse_ok"] = True
            record["semantic_ok"] = normalized == expected
        except Exception as exc:
            record.update(safe_error(exc))
        record["elapsed_seconds"] = round(time.monotonic() - started, 3)
        after = snapshot(workspace)
        record["changed_paths"] = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
        record["native_tool_events"] = {k: v for k, v in events.items() if k in NATIVE_TOOL_EVENTS}
        record["text_delta_events"] = events["text-delta"]
        record["passed"] = bool(record.get("semantic_ok") and not record["changed_paths"] and not record["native_tool_events"])
        if not record["passed"] and text:
            # Only synthetic-case model text, never raw SDK exception messages.
            record["failed_output_sample"] = (text.replace(key, "[REDACTED]") if key else text)[:2000]
        results.append(record)
        if not record["passed"] or not fixture.get("followup") or turn == 1:
            break
        followup = fixture["followup"]
        calls = [item for item in parsed if item["type"] == "function_call"]
        if len(calls) != 1:
            raise ValueError("Roundtrip fixture must produce exactly one call")
        prompt = json.dumps({"type": "function_call_output", "call_id": calls[0]["call_id"],
                             "output": followup["output"]}) + "\n" + followup["request"]
        expected = followup["expected"]
    return results


def proxychains_binary():
    # systemd's PATH may resolve a different binary/config than the shell.
    override = os.environ.get("CURSOR_FALLBACK_PROXYCHAINS")
    local = Path.home() / ".local/bin/proxychains4"
    selected = override or (str(local) if local.is_file() else shutil.which("proxychains4"))
    if not selected or not Path(selected).is_absolute() or not os.access(selected, os.X_OK):
        raise RuntimeError("Set CURSOR_FALLBACK_PROXYCHAINS to an executable absolute path")
    return selected


def bridge_command(route, transport):
    from cursor_sdk._vendor import resolve_bridge_path

    bundle = Path(resolve_bridge_path()).parent.parent
    # Own Node directly, so forced cleanup also terminates the actual bridge.
    command = [str(bundle / "bin" / "node")]
    if transport == "http1":
        command += ["--import", str(Path(__file__).with_name("sdk_http1.mjs"))]
    command += [str(bundle / "dist" / "bin" / "cursor-sdk-bridge.js")]
    if route != "ambient":
        command = ["env"] + [part for name in PROXY_VARS for part in ("-u", name)] + (
            [proxychains_binary(), "-q"] if route == "proxychains" else []) + command
    return command


def run_live(route, key, rounds, fixtures, timeout_seconds=120, transport="default",
             shared_workspace=False, progress=None):
    from cursor_sdk import AgentOptions, LocalAgentOptions, SendOptions

    report = {"model": MODEL, "route": route, "sdk_version": importlib.metadata.version("cursor-sdk"),
              "live_inference": False, "rounds": rounds, "timeout_seconds": timeout_seconds,
              "transport_preference": transport, "results": [], "tool_probe_passed": False,
              "workspace_policy": "shared_fresh_agents" if shared_workspace else "separate_fresh_agents",
              "active_operation": "bridge_launch"}
    command = bridge_command(route, transport)
    try:
        with tempfile.TemporaryDirectory(prefix="cursor-sdk-tools-") as directory:
            root = Path(directory)
            bridge_workspace, state = root / "bridge", root / "state"
            bridge_workspace.mkdir()
            state.mkdir(mode=0o700)
            with managed_client(command, bridge_workspace, state) as client:
                # Preserve the user's high alias; SDK catalogs expose base ID + params.
                report["active_operation"] = "catalog"
                with deadline(40):
                    models = client.models.list(api_key=key)
                report["requested_model_listed"] = any(model.id == MODEL for model in models)
                selection = resolve_model(models)
                report["sdk_selection"] = selection
                if shared_workspace:
                    (root / "cases").mkdir()
                for repetition in range(rounds):
                    for fixture in fixtures["cases"]:
                        workspace = root / ("cases" if shared_workspace else f"case-{repetition}-{fixture['name']}")
                        if not shared_workspace:
                            workspace.mkdir()
                        initial = snapshot(workspace)
                        report["active_operation"] = "agent_create"
                        with deadline(30):
                            agent = client.agents.create(AgentOptions(
                                api_key=key, model=selection, tools=[],
                                disallowed_tools=["mcp", "task", "shell"],
                                local=LocalAgentOptions(cwd=workspace, setting_sources=[]),
                            ))
                        primary = None
                        try:
                            if snapshot(workspace) != initial:
                                report["blocked"] = "Workspace changed during agent creation"
                                return report
                            def send(prompt, observe):
                                report["live_inference"] = True
                                report["active_operation"] = "send"
                                with deadline(timeout_seconds):
                                    return wait_with_diagnostics(agent.send(
                                        prompt, SendOptions(on_step=observe, on_delta=observe)))
                            results = exercise(send, workspace, fixture, fixtures["tools"], key, selection)
                            for record in results:
                                record["repetition"] = repetition + 1
                            report["results"].extend(results)
                            if progress:
                                progress({"repetition": repetition + 1, "case": fixture["name"],
                                          "recorded_turns": len(report["results"]),
                                          "passed_turns": sum(item["passed"] for item in report["results"])})
                        except BaseException as exc:
                            primary = exc
                            exc.spike_operation = report["active_operation"]
                            raise
                        finally:
                            report["active_operation"] = "agent_close"
                            try:
                                with deadline(15):
                                    agent.close()
                            except Exception as exc:
                                exc.spike_operation = "agent_close"
                                if primary is not None:
                                    primary.spike_cleanup_errors = [safe_error(exc)]
                                else:
                                    raise
                        if snapshot(workspace) != initial:
                            report["blocked"] = "Workspace changed during agent lifetime"
                            return report
                        if any(item.get("sdk_status", "finished") != "finished"
                               or ("error_type" in item and "sdk_status" not in item)
                               or item.get("http_status") in (401, 403) or item.get("error_type") == "DeadlineExpired"
                               or item.get("sdk_error_code") in ("unauthenticated", "permission_denied")
                               or item.get("error_type") in ("AuthenticationError", "PermissionDeniedError")
                               or item["native_tool_events"] or item["changed_paths"] for item in results):
                            report["blocked"] = "SDK runtime error, authentication, timeout, or native side effects; stopped without retry"
                            return report
                report["active_operation"] = "bridge_close"
        report["active_operation"] = "finished"
    except Exception as exc:
        report.update(safe_error(exc))
        report.setdefault("failed_operation", report["active_operation"])
    return report


def summarize(report, expected_turns):
    results = report["results"]
    report["expected_turns"] = expected_turns
    report["completed_turns"] = len(results)
    report["passed_turns"] = sum(item["passed"] for item in results)
    report["first_pass_rate"] = report["passed_turns"] / expected_turns
    report["tool_probe_passed"] = bool(report.get("live_inference") and not report.get("blocked")
                                   and not report.get("error_type") and expected_turns >= 20
                                   and report["passed_turns"] == expected_turns)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Actually send paid inference after user authorization")
    parser.add_argument("--route", choices=("ambient", "direct", "proxychains"), default="ambient")
    parser.add_argument("--rounds", type=int, choices=range(1, 6), default=3)
    parser.add_argument("--timeout-seconds", type=int, default=120, help="Wall-clock limit per model turn (1–600)")
    parser.add_argument("--transport", choices=("default", "http1"), default="default",
                        help="SDK transport preference; server policy can override HTTP/1.1")
    parser.add_argument("--shared-workspace", action="store_true",
                        help="Fresh agents in one empty workspace; reuse the SDK model-list cache")
    parser.add_argument("--progress", action="store_true", help="Write safe per-case progress to stderr")
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--output", type=Path, help="Safe report path; refuses to overwrite")
    args = parser.parse_args()
    if not 1 <= args.timeout_seconds <= 600:
        parser.error("--timeout-seconds must be between 1 and 600")
    fixtures = strict_json(FIXTURES.read_text())
    expected_turns = sum(1 + bool(case.get("followup")) for case in fixtures["cases"]) * args.rounds
    if not args.run:
        print(json.dumps({"live_inference": False, "tool_probe_passed": False,
                          "planned_turns": expected_turns, "cases": [c["name"] for c in fixtures["cases"]]}))
        return 0
    key = args.key_file.read_text().strip() if args.key_file else os.environ.get("CURSOR_API_KEY")
    if not key:
        parser.error("Set CURSOR_API_KEY or --key-file; never paste the key into chat")
    if args.output and args.output.exists():
        parser.error("Output already exists")
    progress = (lambda event: print(json.dumps(event), file=sys.stderr, flush=True)) if args.progress else None
    report = summarize(run_live(args.route, key, args.rounds, fixtures, args.timeout_seconds, args.transport,
                                args.shared_workspace, progress), expected_turns)
    text = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        with args.output.open("x") as out:
            out.write(text)
    print(text, end="")
    return 0 if report["tool_probe_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
