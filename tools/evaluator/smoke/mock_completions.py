"""Local OpenAI-compatible streaming endpoint for the manual smoke test."""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class CompletionHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            request = json.loads(self.rfile.read(length))
        except ValueError, json.JSONDecodeError:
            self.send_error(400, "invalid JSON request")
            return
        if request.get("stream") is True:
            body = (
                b'data: {"id":"smoke","choices":[{"index":0,"delta":'
                b'{"content":"smoke reply"},"finish_reason":null}]}\n\n'
                b'data: {"id":"smoke","choices":[{"index":0,"delta":'
                b'{},"finish_reason":"stop"}]}\n\n'
                b"data: [DONE]\n\n"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = json.dumps(
                {
                    "id": "smoke",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "smoke-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "smoke reply"},
                            "finish_reason": "stop",
                        }
                    ],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        print(f"mock completions: {format % args}")


if __name__ == "__main__":
    port = int(os.environ.get("JCODE_MOCK_PORT", "8766"))
    server = ThreadingHTTPServer(("127.0.0.1", port), CompletionHandler)
    print(f"mock OpenAI-compatible API listening at http://127.0.0.1:{port}/v1")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
