"""Provider exchange files: credential-free export and import."""

import os
from dataclasses import replace

import pytest

from vibepod.core import providers
from vibepod.core.providers import ModelSettings, Provider

# The provider store enforces POSIX owner-only permissions and refuses other platforms.
pytestmark = pytest.mark.skipif(os.name == "nt", reason="provider store is POSIX-only")


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "providers"
    monkeypatch.setenv("VP_PROVIDERS_DIR", str(root))
    return root


def hosted():
    return Provider(
        "hosted",
        "openai-chat",
        "https://api.example.com/v1",
        auth="key",
        models=("a", "b"),
        default_model="b",
        model_settings={
            "b": ModelSettings(context_window=128000, reasoning_levels=("low", "high")),
        },
    )


def test_render_exchange_has_no_credentials(store):
    providers.save_provider(hosted(), key="sk-secret-value")
    text = providers.render_exchange(providers.load_provider("hosted"))
    assert text.startswith("# VibePod provider definition")
    assert "sk-secret-value" not in text
    assert "credential_file" not in text
    assert 'auth = "key"' in text and 'default_model = "b"' in text
    assert '[model_settings."b"]' in text and "context_window = 128000" in text


def test_provider_from_toml_round_trips_and_ignores_credential_reference(store):
    original = hosted()
    text = providers.render_exchange(original).replace(
        'auth = "key"',
        'credential_file = "credentials-0123456789abcdef0123456789abcdef.json"\nauth = "key"',
    )
    parsed = providers.provider_from_toml(text, name="copy")
    assert parsed.name == "copy"
    assert parsed.credential_file == "credentials.json"
    assert parsed.models == original.models
    assert parsed.model_settings == original.model_settings
    assert parsed.auth == "key"


@pytest.mark.parametrize(
    "text, message",
    [
        ('version = 2\nname = "x"\n', "version"),
        ("not toml [", "Invalid provider file"),
        (
            'version = 1\nname = "x"\nprotocol = "openai-chat"\n'
            'base_url = "https://e/v1"\nextra = 1\n',
            "Invalid provider file",
        ),
        ('version = 1\nname = "x"\nprotocol = "openai-chat"\nbase_url = "ftp://e"\n', "HTTP"),
    ],
)
def test_provider_from_toml_rejects_bad_files(text, message):
    with pytest.raises(ValueError, match=message):
        providers.provider_from_toml(text)


@pytest.fixture
def file_server():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    responses = []
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            status, body, headers = responses.pop(0)
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{http.server_port}", responses, calls
    http.shutdown()
    http.server_close()
    thread.join()


def test_fetch_exchange_reads_local_files(tmp_path):
    from vibepod.core.provider_exchange import fetch_exchange

    path = tmp_path / "llmapi.toml"
    path.write_text("version = 1\n")
    assert fetch_exchange(str(path)) == ("version = 1\n", False)
    with pytest.raises(ValueError, match="not found"):
        fetch_exchange(str(tmp_path / "missing.toml"))
    with pytest.raises(ValueError, match="scheme"):
        fetch_exchange("ftp://example.com/x.toml")


def test_fetch_exchange_over_http_flags_insecure_transport(file_server):
    from vibepod.core.provider_exchange import fetch_exchange

    url, responses, calls = file_server
    responses.append((200, b"version = 1\n", {"Content-Type": "text/plain"}))
    assert fetch_exchange(url + "/vibepod/provider.toml") == ("version = 1\n", True)
    assert calls == ["/vibepod/provider.toml"]


@pytest.mark.parametrize(
    "status, body, headers, message",
    [
        (302, b"", {"Location": "http://example.com/other"}, "redirect"),
        (404, b'{"error": "leaked-secret"}', {}, "HTTP 404"),
        (200, b"\xff\xfe", {}, "UTF-8"),
        (200, b"x" * (256 * 1024 + 1), {}, "size limit"),
    ],
)
def test_fetch_exchange_refuses_bad_responses(file_server, status, body, headers, message):
    from vibepod.core.provider_exchange import fetch_exchange

    url, responses, calls = file_server
    responses.append((status, body, headers))
    with pytest.raises(ValueError, match=message) as caught:
        fetch_exchange(url + "/provider.toml")
    assert "leaked-secret" not in str(caught.value)
    assert len(calls) == 1


# ---- commands ----

from typer.testing import CliRunner  # noqa: E402

from vibepod.cli import app  # noqa: E402

runner = CliRunner()


def test_export_writes_credential_free_file_to_stdout_and_path(store, tmp_path):
    providers.save_provider(hosted(), key="sk-secret-value")
    result = runner.invoke(app, ["provider", "export", "hosted"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("# VibePod provider definition")
    assert "sk-secret-value" not in result.output and "credential_file" not in result.output
    target = tmp_path / "hosted.toml"
    result = runner.invoke(app, ["provider", "export", "hosted", "-o", str(target)])
    assert result.exit_code == 0, result.output
    assert providers.provider_from_toml(target.read_text(), name="x").models == ("a", "b")
    assert runner.invoke(app, ["provider", "export", "missing"]).exit_code == 1


def test_round_trip_through_import_under_another_name(store, tmp_path):
    providers.save_provider(hosted(), key="sk-secret-value")
    path = tmp_path / "hosted.toml"
    path.write_text(runner.invoke(app, ["provider", "export", "hosted"]).output)
    result = runner.invoke(
        app,
        ["provider", "import", str(path), "--name", "copy", "--key-env", "COPY_KEY"],
    )
    assert result.exit_code == 0, result.output
    copy = providers.load_provider("copy")
    original = providers.load_provider("hosted")
    assert copy == replace(original, name="copy", auth="env", key_env="COPY_KEY")
    assert "copy" in result.output and "openai-chat" in result.output


def test_import_none_and_env_auth(store, tmp_path):
    path = tmp_path / "local.toml"
    path.write_text(
        'version = 1\nname = "local"\nprotocol = "openai-chat"\n'
        'base_url = "http://192.168.1.10:11434/v1"\nmodels = ["m"]\ndefault_model = "m"\n',
    )
    result = runner.invoke(app, ["provider", "import", str(path)])
    assert result.exit_code == 0, result.output
    assert providers.load_provider("local").auth == "none"
    path.write_text(
        'version = 1\nname = "vendor"\nprotocol = "openai-chat"\n'
        'base_url = "https://api.vendor.example/v1"\nauth = "env"\nkey_env = "VENDOR_KEY"\n'
        'models = ["m"]\n',
    )
    result = runner.invoke(app, ["provider", "import", str(path)])
    assert result.exit_code == 0, result.output
    assert "VENDOR_KEY" in result.output
    assert providers.load_provider("vendor").key_env == "VENDOR_KEY"


def _key_file(tmp_path):
    path = tmp_path / "hosted.toml"
    path.write_text(providers.render_exchange(hosted()))
    return path


def test_import_key_auth_non_interactive_needs_key_env(store, tmp_path):
    result = runner.invoke(app, ["provider", "import", str(_key_file(tmp_path))])
    assert result.exit_code == 1
    assert "--key-env" in result.output
    assert not (store / "hosted").exists()


def test_import_key_auth_prompts_and_stores_the_key(store, tmp_path, monkeypatch):
    monkeypatch.setattr("vibepod.commands.provider._interactive", lambda: True)
    result = runner.invoke(
        app,
        ["provider", "import", str(_key_file(tmp_path))],
        input="sk-typed\ny\n",
    )
    assert result.exit_code == 0, result.output
    assert "sk-typed" not in result.output
    assert providers.resolve_key(providers.load_provider("hosted")) == "sk-typed"
    # Declining plaintext storage writes nothing.
    result = runner.invoke(
        app,
        ["provider", "import", str(_key_file(tmp_path)), "--name", "other"],
        input="sk-typed\nn\n",
    )
    assert result.exit_code != 0
    assert not (store / "other").exists()


def test_import_refuses_existing_name_without_touching_it(store, tmp_path):
    providers.save_provider(hosted(), key="sk-secret-value")
    before = providers.load_provider("hosted")
    result = runner.invoke(
        app,
        ["provider", "import", str(_key_file(tmp_path)), "--key-env", "X"],
    )
    assert result.exit_code == 1 and "already exists" in result.output
    assert providers.load_provider("hosted") == before
    assert providers.resolve_key(before) == "sk-secret-value"


def test_import_from_http_url_warns_about_transport(store, file_server):
    url, responses, _ = file_server
    responses.append((200, providers.render_exchange(hosted()).encode(), {}))
    result = runner.invoke(
        app,
        ["provider", "import", url + "/provider.toml", "--key-env", "HOSTED_KEY"],
    )
    assert result.exit_code == 0, result.output
    assert "without TLS" in result.output
    assert providers.load_provider("hosted").base_url == "https://api.example.com/v1"


def test_import_rejects_key_env_for_other_auth_modes(store, tmp_path):
    path = tmp_path / "local.toml"
    path.write_text(
        'version = 1\nname = "local"\nprotocol = "openai-chat"\n'
        'base_url = "http://192.168.1.10:11434/v1"\nmodels = ["m"]\n',
    )
    result = runner.invoke(app, ["provider", "import", str(path), "--key-env", "X"])
    assert result.exit_code == 1 and "--key-env" in result.output
    assert not (store / "local").exists()


def test_non_ascii_model_ids_survive_store_and_exchange(store, tmp_path, monkeypatch):
    providers.save_provider(
        Provider("local", "openai-chat", "http://localhost:11434/v1", models=("modèle-ü", "plain")),
    )
    assert providers.load_provider("local").models == ("modèle-ü", "plain")
    text = providers.render_exchange(providers.load_provider("local"))
    assert "modèle-ü" in text
    path = tmp_path / "local.toml"
    path.write_text(text, encoding="utf-8")
    result = runner.invoke(app, ["provider", "import", str(path), "--name", "copy"])
    assert result.exit_code == 0, result.output
    assert providers.load_provider("copy").models == ("modèle-ü", "plain")
