"""
Overlay server: serves the HTML browser source over HTTP and pushes
translated chat events over a WebSocket connection.
"""

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional
import websockets
from websockets.server import WebSocketServerProtocol

_clients: set[WebSocketServerProtocol] = set()
_loop: Optional[asyncio.AbstractEventLoop] = None


# ── WebSocket server ────────────────────────────────────────────────────────

async def _ws_handler(ws: WebSocketServerProtocol):
    _clients.add(ws)
    try:
        await ws.wait_closed()
    finally:
        _clients.discard(ws)


async def _serve_ws(host: str, port: int):
    global _loop
    _loop = asyncio.get_running_loop()
    async with websockets.serve(_ws_handler, host, port):
        await asyncio.Future()  # run forever


def _ws_thread(host: str, port: int):
    asyncio.run(_serve_ws(host, port))


def start_ws_server(host: str = "localhost", port: int = 8765):
    t = threading.Thread(target=_ws_thread, args=(host, port), daemon=True)
    t.start()
    # Give the event loop a moment to start
    time.sleep(0.3)


def broadcast(payload: dict):
    """Thread-safe: enqueue a message to all connected WebSocket clients."""
    if _loop is None or not _clients:
        return
    data = json.dumps(payload)

    async def _send():
        dead = set()
        for ws in list(_clients):
            try:
                await ws.send(data)
            except Exception:
                dead.add(ws)
        _clients.difference_update(dead)

    asyncio.run_coroutine_threadsafe(_send(), _loop)


# ── HTTP server ─────────────────────────────────────────────────────────────

_OVERLAY_HTML = Path(__file__).parent / "overlay.html"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress access log

    def do_GET(self):
        if self.path in ("/", "/overlay.html"):
            self._serve_file(_OVERLAY_HTML, "text/html; charset=utf-8")
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_file(self, path: Path, content_type: str):
        try:
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()


def start_http_server(host: str = "localhost", port: int = 8766):
    server = HTTPServer((host, port), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"[overlay] Browser source: http://{host}:{port}/")
    return server
