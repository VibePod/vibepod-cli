"""Real local HTTP fixture: no live provider or credentials required."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from vibepod.core import provider_discovery as discovery
from vibepod.core.providers import Provider


@pytest.fixture
def server():
    calls = []
    responses = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append((self.path, dict(self.headers)))
            status, body, headers = responses.pop(0)
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{http.server_port}", calls, responses
    http.shutdown()
    http.server_close()
    thread.join()


def test_openai_models_preserve_base_path(server):
    url, calls, responses = server
    responses.append((200, {"data": [{"id": "b"}, {"id": "a"}, {"id": "a"}]}, {}))
    p = Provider("local", "openai-chat", url + "/custom/v1/")
    assert discovery.discover_models(p) == ["a", "b"]
    assert calls[0][0] == "/custom/v1/models"
    assert "Authorization" not in calls[0][1]


def test_anthropic_base_path_gets_v1_models(server):
    url, calls, responses = server
    responses.append((200, {"data": [{"id": "a"}]}, {}))
    assert discovery.discover_models(Provider("local", "anthropic", url + "/proxy")) == ["a"]
    assert calls[0][0] == "/proxy/v1/models"


def test_http_key_requires_consent(server):
    url, calls, _ = server
    with pytest.raises(ValueError, match="HTTP"):
        discovery.discover_models(Provider("local", "openai-chat", url), key="secret")
    assert calls == []


def test_anthropic_pagination_and_headers(server):
    url, calls, responses = server
    responses.extend(
        [
            (200, {"data": [{"id": "a"}], "has_more": True, "last_id": "a"}, {}),
            (200, {"data": [{"id": "b"}], "has_more": False}, {}),
        ],
    )
    p = Provider("local", "anthropic", url)
    assert discovery.discover_models(p, key="secret", allow_http_key=True) == ["a", "b"]
    assert calls[0][0] == "/v1/models"
    assert calls[0][1]["X-Api-Key"] == "secret"
    assert calls[1][0] == "/v1/models?after_id=a"


@pytest.mark.parametrize(
    "status, message",
    [(401, "Authentication"), (403, "Authentication"), (404, "manual"), (500, "HTTP 500")],
)
def test_error_does_not_echo_response_body(server, status, message):
    url, _, responses = server
    responses.append((status, {"error": "leaked-secret"}, {}))
    with pytest.raises(ValueError, match=message) as caught:
        discovery.discover_models(Provider("local", "openai-chat", url))
    assert "leaked-secret" not in str(caught.value)


def test_malformed_http_response_is_a_clean_error(monkeypatch):
    from http.client import BadStatusLine

    class Opener:
        def open(self, request, timeout):
            raise BadStatusLine("junk")

    monkeypatch.setattr(discovery, "build_opener", lambda *handlers: Opener())
    with pytest.raises(ValueError, match="invalid HTTP") as caught:
        discovery.discover_models(Provider("local", "openai-chat", "http://localhost:1/v1"))
    assert "junk" not in str(caught.value)


def test_redirect_not_followed(server):
    url, calls, responses = server
    responses.append((302, {}, {"Location": url + "/other"}))
    with pytest.raises(ValueError, match="redirect"):
        discovery.discover_models(Provider("local", "openai-chat", url))
    assert len(calls) == 1


def test_invalid_model_data_rejected(server):
    url, _, responses = server
    responses.append((200, {"data": [{"name": "not-an-id"}]}, {}))
    with pytest.raises(ValueError, match="model"):
        discovery.discover_models(Provider("local", "openai-chat", url))


def test_repeated_pagination_cursor_rejected(server):
    url, calls, responses = server
    page = {"data": [{"id": "a"}], "has_more": True, "last_id": "a"}
    responses.extend([(200, page, {}), (200, page, {})])
    with pytest.raises(ValueError, match="pagination"):
        discovery.discover_models(Provider("local", "anthropic", url))
    assert len(calls) == 2
