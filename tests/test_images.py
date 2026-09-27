"""Image input: validation, attachment limits and SDK message shape; no network or real SDK."""
import base64
import http.client
import http.server
import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest
import zlib

from cursor_sdk_bridge.cursor_sdk2api import Service, make_server
from cursor_sdk_bridge.probe_service import PALETTE, ProbeFailed, probe_image
from cursor_sdk_bridge.request_log import RequestLog
from cursor_sdk_bridge.responses_protocol import (ATTACHED_IMAGE, IMAGE_NOTE, MAX_IMAGE_BYTES, OMITTED_IMAGE, image_dimension,
                                image_format, request_payload)
from cursor_sdk_bridge.models import MODELS
from cursor_sdk_bridge.sdk_backend import SDKBackend
from cursor_sdk_bridge.switch_config import ServiceNotReady, service_report, verify_service
from test_cursor_sdk2api import MODEL, StubSDK
from test_sdk_backend import StubClient

OK = '{"output":[{"type":"message","text":"ok"}]}'
VIEW_IMAGE = {"type": "function", "name": "view_image", "parameters": {"type": "object", "properties": {
    "path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}}
CATALOG = Path(__file__).resolve().parent.parent / "cursor_sdk_bridge/assets/models.json"


def png(width=2, height=1, rgb=(255, 0, 0)):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = b"".join(bytes(1) + bytes(rgb) * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (bytes.fromhex("89504e470d0a1a0a") + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows))
            + chunk(b"IEND", b""))


def encoded(raw):
    return base64.b64encode(raw).decode()


def image(raw=None, mime="image/png"):
    return {"type": "input_image", "image_url": f"data:{mime};base64," + encoded(raw or png()), "detail": "auto"}


def webp(chunk, body):
    return b"RIFF" + struct.pack("<I", 12 + len(body)) + b"WEBP" + chunk + struct.pack("<I", len(body)) + body


class ImageSDK(StubSDK):
    def __init__(self):
        super().__init__()
        self.images, self.raw_prompts = [], []

    async def generate(self, model, prompt, images=()):
        self.images.append(list(images))
        self.raw_prompts.append(prompt)
        return await super().generate(model, prompt)


class ImageRequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.logdir = Path(self.temp.name) / "logs"
        self.sdk = ImageSDK()
        self.service = Service(self.sdk, log=RequestLog(self.logdir))
        self.server = make_server(self.service, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.service.close()
        self.temp.cleanup()

    def post(self, body):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        connection.request("POST", "/v1/responses", json.dumps(body), {"Content-Type": "application/json"})
        response = connection.getresponse()
        status, content = response.status, response.read().decode()
        connection.close()
        return status, json.loads(content)

    def ask(self, content, **extra):
        return self.post({"model": MODEL, "input": [{"role": "user", "content": content}], **extra})

    def log_entries(self, count):
        path = self.logdir / "requests.jsonl"
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not (path.exists() and len(path.read_text().splitlines()) >= count):
            time.sleep(0.02)
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_user_image_is_attached_and_prompt_keeps_only_a_placeholder(self):
        raw = png(4, 3)
        self.sdk.results = [OK]
        status, _ = self.ask([{"type": "input_text", "text": "What color?"}, image(raw)])
        self.assertEqual(status, 200)
        self.assertEqual(self.sdk.images, [[{"data": encoded(raw), "mime_type": "image/png",
                                             "dimension": {"width": 4, "height": 3}, "bytes": len(raw)}]])
        self.assertEqual(self.sdk.prompts[0]["input"][0]["content"][1],
                         {"type": "input_text", "text": ATTACHED_IMAGE.format(1)})
        self.assertNotIn(encoded(raw), self.sdk.raw_prompts[0])
        self.assertIn(IMAGE_NOTE, self.sdk.raw_prompts[0])

    def test_text_only_request_sends_no_images_or_note(self):
        self.sdk.results = [OK]
        status, _ = self.post({"model": MODEL, "input": "hello"})
        self.assertEqual((status, self.sdk.images), (200, [[]]))
        self.assertNotIn(IMAGE_NOTE, self.sdk.raw_prompts[0])

    def test_view_image_function_output_is_attached(self):
        raw = png(rgb=(0, 255, 0))
        self.sdk.results = [OK]
        history = [{"role": "user", "content": "look at the file"},
                   {"type": "function_call", "call_id": "v1", "name": "view_image", "arguments": '{"path":"/tmp/a.png"}'},
                   {"type": "function_call_output", "call_id": "v1", "output": [image(raw)]}]
        status, _ = self.post({"model": MODEL, "input": history, "tools": [VIEW_IMAGE]})
        self.assertEqual(status, 200)
        self.assertEqual([item["data"] for item in self.sdk.images[0]], [encoded(raw)])
        self.assertEqual(self.sdk.prompts[0]["input"][2]["output"],
                         [{"type": "input_text", "text": ATTACHED_IMAGE.format(1)}])

    def test_only_latest_four_images_are_attached_in_order(self):
        raws = [png(rgb=(40 * index, 0, 0)) for index in range(6)]
        self.sdk.results = [OK]
        status, _ = self.post({"model": MODEL, "input": [
            {"role": "user", "content": [image(raw) for raw in raws[:3]]},
            {"role": "assistant", "content": [{"type": "output_text", "text": "seen"}]},
            {"role": "user", "content": [image(raw) for raw in raws[3:]]}]})
        self.assertEqual(status, 200)
        self.assertEqual([item["data"] for item in self.sdk.images[0]], [encoded(raw) for raw in raws[2:]])
        history = self.sdk.prompts[0]["input"]
        placeholders = [part["text"] for item in (history[0], history[2]) for part in item["content"]]
        self.assertEqual(placeholders, [OMITTED_IMAGE] * 2 + [ATTACHED_IMAGE.format(n) for n in range(1, 5)])

    def test_previous_response_keeps_cached_image_attached(self):
        raw = png()
        self.sdk.results = [OK, OK]
        _, first = self.ask([image(raw)])
        status, _ = self.post({"model": MODEL, "previous_response_id": first["id"], "input": "and now?"})
        self.assertEqual(status, 200)
        self.assertEqual([item["data"] for item in self.sdk.images[1]], [encoded(raw)])

    def test_log_counts_images_without_image_data(self):
        raw = png()
        self.sdk.results = [OK]
        self.ask([image(raw)])
        entry = self.log_entries(1)[-1]
        self.assertEqual((entry["outcome"], entry["images"], entry["image_bytes"]), ("completed", 1, len(raw)))
        self.assertNotIn(encoded(raw), (self.logdir / "requests.jsonl").read_text())

    def test_unsupported_images_rejected_before_sdk_without_echo(self):
        secret = encoded(b"secret-not-an-image")
        oversized = encoded(png() + bytes(MAX_IMAGE_BYTES))
        cases = [
            ({"image_url": "https://example.com/secret.png"}, "unsupported_image_source", ".image_url"),
            ({"image_url": "file:///tmp/secret.png"}, "unsupported_image_source", ".image_url"),
            ({"file_id": "file-secret"}, "unsupported_image_source", ".file_id"),
            ({}, "unsupported_image_source", ".image_url"),
            ({"image_url": "data:image/svg+xml;base64," + secret}, "unsupported_image_format", ".image_url"),
            ({"image_url": "data:image/png;base64," + secret}, "unsupported_image_format", ".image_url"),
            ({"image_url": "data:image/png,secret"}, "invalid_image_data", ".image_url"),
            ({"image_url": "data:image/png;base64,secret!!"}, "invalid_image_data", ".image_url"),
            ({"image_url": "data:image/png;base64," + oversized}, "image_too_large", ".image_url")]
        for fields, code, param in cases:
            with self.subTest(code=code, param=param):
                status, result = self.ask([{"type": "input_text", "text": "see"}, {"type": "input_image", **fields}])
                self.assertEqual((status, result["error"]["code"]), (400, code))
                self.assertEqual(result["error"]["param"], "input[0].content[1]" + param)
                self.assertNotIn("secret", json.dumps(result))
        self.assertFalse(self.sdk.prompts)

    def test_invalid_view_image_output_is_rejected_with_its_path(self):
        history = [{"type": "function_call", "call_id": "v1", "name": "view_image", "arguments": '{"path":"/tmp/a.png"}'},
                   {"type": "function_call_output", "call_id": "v1",
                    "output": [{"type": "input_image", "image_url": "https://example.com/a.png"}]}]
        status, result = self.post({"model": MODEL, "input": history, "tools": [VIEW_IMAGE]})
        self.assertEqual((status, result["error"]["param"]), (400, "input[1].output[0].image_url"))
        self.assertFalse(self.sdk.prompts)

    def test_health_advertises_image_inputs(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request("GET", "/health")
        health = json.load(connection.getresponse())
        connection.close()
        self.assertEqual(health["adapter_version"], 3)
        self.assertIn("image_inputs", health["capabilities"])


class DimensionTests(unittest.TestCase):
    def test_every_supported_format_is_detected_with_its_dimension(self):
        cases = [
            (png(4, 3), "image/png", 4, 3),
            (b"GIF89a" + struct.pack("<HH", 3, 5) + bytes(20), "image/gif", 3, 5),
            (bytes.fromhex("ffd8ffe0") + struct.pack(">H", 16) + bytes(14) + bytes.fromhex("ffc0")
             + struct.pack(">HBHH", 17, 8, 7, 9) + bytes(12), "image/jpeg", 9, 7),
            (webp(b"VP8 ", bytes(3) + bytes.fromhex("9d012a") + struct.pack("<HH", 11, 12)), "image/webp", 11, 12),
            (webp(b"VP8L", bytes.fromhex("2f") + (4 | 5 << 14).to_bytes(4, "little") + bytes(5)), "image/webp", 5, 6),
            (webp(b"VP8X", bytes(4) + (9).to_bytes(3, "little") + (19).to_bytes(3, "little")), "image/webp", 10, 20)]
        for raw, mime, width, height in cases:
            with self.subTest(mime=mime, width=width):
                self.assertEqual(image_format(raw), mime)
                self.assertEqual(image_dimension(raw, mime), {"width": width, "height": height})

    def test_truncated_header_omits_dimension(self):
        self.assertIsNone(image_dimension(bytes.fromhex("ffd8ff"), "image/jpeg"))
        self.assertIsNone(image_dimension(b"GIF89a", "image/gif"))


class CatalogTests(unittest.TestCase):
    def test_all_models_declare_image_input(self):
        models = json.loads(CATALOG.read_text())["models"]
        self.assertEqual({model["slug"]: model["input_modalities"] for model in models},
                         {model: ["text", "image"] for model in MODELS})


class CapturingClient(StubClient):
    async def send(self, message, options):
        self.message = message
        return await super().send(message, options)


class BackendImageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        key = root / "key"
        key.write_text("synthetic-test-key")
        key.chmod(0o600)
        self.backend = SDKBackend(key, root / "workspace", timeout=1)
        self.backend.key = "synthetic-test-key"
        self.backend.workspace.mkdir()
        self.client = self.backend.client = CapturingClient()

    async def asyncTearDown(self):
        await self.backend.close()
        self.temp.cleanup()

    async def test_images_are_sent_as_sdk_attachments(self):
        raw = png(4, 3)
        attachment = {"data": encoded(raw), "mime_type": "image/png", "dimension": {"width": 4, "height": 3},
                      "bytes": len(raw)}
        await self.backend.generate(MODEL, "prompt", [attachment])
        self.assertEqual(self.client.message.to_json(), {"text": "prompt", "images": [
            {"data": {"data": encoded(raw), "mimeType": "image/png"}, "dimension": {"width": 4, "height": 3}}]})

    async def test_text_only_prompt_is_sent_as_plain_text(self):
        await self.backend.generate(MODEL, "prompt")
        self.assertEqual(self.client.message, "prompt")


class OldHealthHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        data = json.dumps({"service": "cursor-sdk2api", "status": "ready", "adapter_version": 2,
                           "capabilities": ["namespace_functions"]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ServerCase(unittest.TestCase):
    def serve(self, server, close=None):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join()
            if close:
                close()
        self.addCleanup(stop)
        return server.server_port


class ServicePreflightTests(ServerCase):
    def test_image_capable_adapter_passes_preflight_and_status(self):
        service = Service(StubSDK())
        port = self.serve(make_server(service, 0), service.close)
        verify_service(port)
        self.assertEqual(service_report(port), {"state": "ready", "adapter_version": 3, "image_inputs": True})

    def test_text_only_adapter_is_rejected_and_reported(self):
        port = self.serve(http.server.ThreadingHTTPServer(("127.0.0.1", 0), OldHealthHandler))
        with self.assertRaisesRegex(ServiceNotReady, "image-capable"):
            verify_service(port)
        self.assertEqual(service_report(port), {"state": "ready", "adapter_version": 2, "image_inputs": False})

    def test_unreachable_adapter_is_reported(self):
        spare = socket.socket()
        spare.bind(("127.0.0.1", 0))
        port = spare.getsockname()[1]
        spare.close()
        self.assertEqual(service_report(port)["state"], "unreachable")


def quadrant_colors(raw):
    width, offset, data = struct.unpack(">I", raw[16:20])[0], 8, b""
    while offset < len(raw):
        length = struct.unpack(">I", raw[offset:offset + 4])[0]
        if raw[offset + 4:offset + 8] == b"IDAT":
            data += raw[offset + 8:offset + 8 + length]
        offset += 12 + length
    pixels, stride = zlib.decompress(data), 1 + 3 * width
    names = {rgb: name for name, rgb in PALETTE.items()}

    def color(x, y):
        start = y * stride + 1 + 3 * x
        return names[tuple(pixels[start:start + 3])]
    quarter = width // 4
    return [color(quarter, quarter), color(3 * quarter, quarter), color(quarter, 3 * quarter),
            color(3 * quarter, 3 * quarter)]


class ColorSDK(StubSDK):
    """Reads the attached probe image the way a correct model would."""
    def __init__(self, wrong=False):
        super().__init__()
        self.wrong = wrong

    async def generate(self, model, prompt, images=()):
        self.prompts.append(request_payload(prompt))
        colors = quadrant_colors(base64.b64decode(images[0]["data"]))
        text = " ".join(colors[::-1] if self.wrong else colors)
        return json.dumps({"output": [{"type": "message", "text": text}]})


class ImageProbeTests(ServerCase):
    def run_probe(self, sdk):
        service = Service(sdk)
        return probe_image(self.serve(make_server(service, 0), service.close))

    def test_user_message_and_view_image_sources_are_read_back(self):
        sdk = ColorSDK()
        self.assertEqual(self.run_probe(sdk)["image_sources"], ["user_message", "function_call_output"])
        placeholder = {"type": "input_text", "text": ATTACHED_IMAGE.format(1)}
        self.assertEqual(sdk.prompts[0]["input"][0]["content"][1], placeholder)
        self.assertEqual(sdk.prompts[1]["input"][-1]["output"], [placeholder])

    def test_wrong_reading_fails_without_another_output(self):
        sdk = ColorSDK(wrong=True)
        with self.assertRaises(ProbeFailed):
            self.run_probe(sdk)
        self.assertEqual(len(sdk.prompts), 1)


if __name__ == "__main__":
    unittest.main()
