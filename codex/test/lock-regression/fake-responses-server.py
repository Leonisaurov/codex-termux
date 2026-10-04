#!/usr/bin/env python3
"""Fake OpenAI Responses API server: returns a shell_command tool call once."""
import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
COMMAND = sys.argv[2] if len(sys.argv) > 2 else f"touch {tempfile.gettempdir()}/approved.txt"
LOG = sys.argv[3] if len(sys.argv) > 3 else "/dev/null"

state = {"calls": 0}
lock = threading.Lock()


def sse(events):
    out = []
    for ev in events:
        kind = ev["type"]
        out.append(f"event: {kind}\ndata: {json.dumps(ev)}\n\n")
    return "".join(out).encode()


def tool_call_body(command, call_id="call-1"):
    # El tool de shell en el pin vendeoreado se llama `exec_command` con `cmd`
    # (core/src/tools/handlers/shell_spec.rs:96). `shell_command`/`command` fueron
    # nombres de releases viejos: el router responde "unsupported call" y la
    # aprobación nunca se pide.
    args = json.dumps({"cmd": command, "tty": False})
    return sse([
        {"type": "response.created", "response": {"id": "resp-1"}},
        {"type": "response.output_item.done",
         "item": {"type": "function_call", "call_id": call_id,
                  "name": "exec_command", "arguments": args}},
        {"type": "response.completed",
         "response": {"id": "resp-1", "usage": {"input_tokens": 0, "input_tokens_details": None,
                                                "output_tokens": 0, "output_tokens_details": None,
                                                "total_tokens": 0}}},
    ])


def final_message_body(text="listo"):
    return sse([
        {"type": "response.created", "response": {"id": "resp-2"}},
        {"type": "response.output_item.done",
         "item": {"type": "message", "role": "assistant", "id": "msg-1",
                  "content": [{"type": "output_text", "text": text}]}},
        {"type": "response.completed",
         "response": {"id": "resp-2", "usage": {"input_tokens": 0, "input_tokens_details": None,
                                                "output_tokens": 0, "output_tokens_details": None,
                                                "total_tokens": 0}}},
    ])


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, body, ctype="text/event-stream", code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(json.dumps({"data": [{"id": "fake-model"}]}).encode(), "application/json")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        with open(LOG, "ab") as fh:
            fh.write(b"\n===== REQUEST " + self.path.encode() + b" =====\n" + raw + b"\n")
        with lock:
            state["calls"] += 1
            call_no = state["calls"]
        if "responses" in self.path:
            self._send(tool_call_body(COMMAND))
        else:
            self._send(final_message_body())


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"fake-responses listening on 127.0.0.1:{PORT}", flush=True)
    srv.serve_forever()
