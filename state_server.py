"""
Tiny HTTP server that serves state.json at GET /state.
Start it as a daemon thread from bot.py so the live visualizer can poll it.
"""

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from state_writer import STATE_PATH

log = logging.getLogger(__name__)

_DEFAULT_PORT = int(os.getenv("STATE_SERVER_PORT", "8081"))


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/state":
            try:
                with open(STATE_PATH, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
            except FileNotFoundError:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'{"error": "state not ready"}')
        elif self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_):
        pass  # silence per-request logs


def start(port: int = _DEFAULT_PORT) -> None:
    server = HTTPServer(("0.0.0.0", port), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    log.info("State server listening on :%d", port)
