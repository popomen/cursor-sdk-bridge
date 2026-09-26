#!/usr/bin/env python3
"""Read-only SDK catalog probe. Output contains no credentials or raw errors."""
import argparse
from dataclasses import asdict
import importlib.metadata
import json
import os
from pathlib import Path
import tempfile
import time

PROXY_VARS = ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")


def probe(route, key):
    from cursor_sdk import CursorClient, DefaultHttpxClient
    from cursor_sdk._vendor import resolve_bridge_path

    result = dict(route=route, credential_present=bool(key),
                  sdk_version=importlib.metadata.version("cursor-sdk"))
    command = [resolve_bridge_path()]
    if route != "ambient":
        command = ["env"] + [part for name in PROXY_VARS for part in ("-u", name)] + (
            ["proxychains4", "-q"] if route == "proxychains" else []
        ) + command
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="cursor-sdk-catalog-") as directory:
            root = Path(directory)
            workspace, state = root / "workspace", root / "state"
            workspace.mkdir()
            state.mkdir(mode=0o700)
            with CursorClient.launch_bridge(
                command=command, workspace=workspace, state_root=state,
                timeout=15, client_timeout=35, max_retries=0,
                http_client=DefaultHttpxClient(trust_env=False, timeout=35),
            ) as client:
                result["bridge_version"] = client.get_version().get("bridgeVersion")
                models = client.models.list(api_key=key)
                ids = sorted(model.id for model in models)
                result.update(status="ok", model_count=len(ids), model_ids=ids,
                              opus_ids=[i for i in ids if "opus" in i.lower()],
                              opus_models=[{field: asdict(model)[field]
                                            for field in ("id", "parameters", "variants")}
                                           for model in models if "opus" in model.id.lower()])
    except Exception as exc:
        result.update(status="error", error_type=type(exc).__name__,
                      http_status=getattr(exc, "status_code", None))
        # Classify locally: raw exceptions may contain credential-bearing URLs.
        message = str(exc).lower()
        result["error_flags"] = {name: needle in message for name, needle in (
            ("api_key_required", "api key is required"),
            ("invalid_api_key", "invalid user api key"),
            ("unauthenticated", "unauthenticated"),
            ("timeout", "timed out"),
            ("proxy_tunnel", "connect tunnel"),
        )}
    result["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--route", choices=("ambient", "direct", "proxychains"), required=True)
    parser.add_argument("--key-file", type=Path, help="Read an SDK key without putting it in argv")
    parser.add_argument("--allow-missing-key", action="store_true", help="Reproduce unauthenticated calls")
    parser.add_argument("--repeat", type=int, default=1, choices=range(1, 4))
    parser.add_argument("--output", type=Path, help="Write safe JSON; refuses to overwrite")
    args = parser.parse_args()
    key = args.key_file.read_text().strip() if args.key_file else os.environ.get("CURSOR_API_KEY")
    if not key and not args.allow_missing_key:
        parser.error("Set CURSOR_API_KEY or use --key-file; never paste the key into chat")
    if args.output and args.output.exists():
        parser.error("Output already exists; choose a new path")
    results = []
    for _ in range(args.repeat):
        result = probe(args.route, key)
        results.append(result)
        if result["status"] != "ok":
            break
    text = json.dumps(results, indent=2) + "\n"
    if args.output:
        with args.output.open("x") as out:
            out.write(text)
    print(text, end="")
    return 0 if all(result["status"] == "ok" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
