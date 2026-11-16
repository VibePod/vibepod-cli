from __future__ import annotations

import http.client
import json
import threading

import pytest

from vibepod.core.subscription_usage import UsageStore, Window
from vibepod.core.usage_dashboard import make_server


@pytest.fixture
def dashboard(tmp_path):
    store = UsageStore(tmp_path / "usage.db")
    store.record("codex", "default", [Window("Codex primary", 25)])
    server = make_server(store, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def request(server, path="/api/usage", method="GET", headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    try:
        conn.request(method, path, headers=headers or {})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def test_dashboard_serves_only_snapshots_and_bundled_assets(dashboard):
    status, headers, body = request(dashboard)
    assert status == 200 and headers["Cache-Control"] == "no-store"
    assert "Access-Control-Allow-Origin" not in headers
    assert json.loads(body)["providers"][0]["windows"][0]["used_percent"] == 25
    for path in ("/", "/app.js", "/style.css"):
        assert request(dashboard, path)[0] == 200
    for path in (
        "/../agents/codex/auth.json",
        "/subscriptions.db",
        "/api/refresh",
        "/api/usage?file=auth.json",
    ):
        assert request(dashboard, path)[0] == 404
    assert request(dashboard, method="POST")[0] == 501


def test_dashboard_rejects_cross_origin_and_dns_rebinding(dashboard):
    assert dashboard.server_address[0] == "127.0.0.1"
    assert request(dashboard, headers={"Host": "attacker.example"})[0] == 403
    assert request(dashboard, headers={"Origin": "https://attacker.example"})[0] == 403
    assert request(dashboard, headers={"Origin": "null"})[0] == 403
    origin = f"http://127.0.0.1:{dashboard.server_port}"
    assert request(dashboard, headers={"Origin": origin})[0] == 200
