"""Loopback-only, read-only quota UI. No Docker, credentials, or raw-log access."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any, cast

from vibepod.core.subscription_usage import UsageStore


def make_server(store: UsageStore, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            # Never log request paths, headers, or incidental browser data.
            pass

        def do_GET(self) -> None:
            actual_port = cast(ThreadingHTTPServer, self.server).server_port
            allowed = {f"127.0.0.1:{actual_port}", f"localhost:{actual_port}"}
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            if host not in allowed or (origin is not None and origin != f"http://{host}"):
                self.send_error(403)
                return
            resources = {
                "/": ("usage-dashboard.html", "text/html; charset=utf-8"),
                "/app.js": ("usage-dashboard.js", "text/javascript; charset=utf-8"),
                "/style.css": ("usage-dashboard.css", "text/css; charset=utf-8"),
            }
            if self.path == "/api/usage":
                try:
                    body = json.dumps(store.snapshot(), allow_nan=False).encode()
                except Exception:
                    self.send_error(503, "Usage data unavailable")
                    return
                content_type = "application/json"
            elif self.path in resources:
                name, content_type = resources[self.path]
                body = files("vibepod.resources").joinpath(name).read_bytes()
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
