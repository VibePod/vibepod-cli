"""Real agent requests against a local fake API; no paid services or real keys.

Run explicitly with VP_PROVIDER_SMOKE=1 and VP_PROVIDER_SMOKE_AGENT=<agent> (one of
pi, codex, qwen, tau, jcode, opencode).
The dedicated Actions job builds the companion agent image before this test.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import docker
import pytest

from vibepod.core.provider_launch import prepare_provider
from vibepod.core.provider_runtime import WRAPPED_AGENTS, bootstrap_volume, wrap_provider_command
from vibepod.core.providers import Provider, save_provider

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("VP_PROVIDER_SMOKE") != "1",
        reason="Dedicated provider CI job",
    ),
]


def response_events(model):
    message = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "provider-smoke-ok", "annotations": []}],
    }
    response = {
        "id": "resp_test",
        "object": "response",
        "created_at": 1,
        "model": model,
        "status": "in_progress",
        "output": [],
    }
    yield "response.created", {"response": response}
    yield (
        "response.output_item.added",
        {"output_index": 0, "item": {**message, "status": "in_progress", "content": []}},
    )
    yield (
        "response.content_part.added",
        {
            "item_id": "msg_test",
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        },
    )
    yield (
        "response.output_text.delta",
        {
            "item_id": "msg_test",
            "output_index": 0,
            "content_index": 0,
            "delta": "provider-smoke-ok",
        },
    )
    yield (
        "response.output_text.done",
        {"item_id": "msg_test", "output_index": 0, "content_index": 0, "text": "provider-smoke-ok"},
    )
    yield (
        "response.content_part.done",
        {
            "item_id": "msg_test",
            "output_index": 0,
            "content_index": 0,
            "part": message["content"][0],
        },
    )
    yield "response.output_item.done", {"output_index": 0, "item": message}
    yield (
        "response.completed",
        {
            "response": {
                **response,
                "status": "completed",
                "output": [message],
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "total_tokens": 2,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        },
    )


def test_agent_routes_to_selected_provider_without_profile_writes(tmp_path, monkeypatch):
    agent = os.environ["VP_PROVIDER_SMOKE_AGENT"]
    assert agent in {"pi", "codex", "qwen", "tau", "jcode", "opencode"}
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, payload, self.headers.get("Authorization")))
            if self.path.endswith("/chat/completions"):

                def chunk(delta, reason):
                    return {
                        "id": "chatcmpl-test",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": payload["model"],
                        "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
                    }

                body = (
                    "".join(
                        "data: " + json.dumps(c) + "\n\n"
                        for c in [
                            chunk({"role": "assistant", "content": "provider-smoke-ok"}, None),
                            chunk({}, "stop"),
                        ]
                    )
                    + "data: [DONE]\n\n"
                )
            elif self.path.endswith("/responses"):
                body = "".join(
                    f"event: {event}\ndata: "
                    + json.dumps({"type": event, "sequence_number": i, **data})
                    + "\n\n"
                    for i, (event, data) in enumerate(response_events(payload["model"]))
                )
            else:
                self.send_error(404)
                return
            raw = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = docker.from_env()
    container = None
    try:
        monkeypatch.setenv("VP_PROVIDERS_DIR", str(tmp_path / "providers"))
        monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path / "config"))
        url = f"http://host.docker.internal:{server.server_port}/v1"
        if agent == "jcode":
            # jcode accepts plain http only for localhost/private IPs, not hostnames.
            gateway = client.networks.get("bridge").attrs["IPAM"]["Config"][0]["Gateway"]
            url = f"http://{gateway}:{server.server_port}/v1"
        protocol = "openai-responses" if agent == "codex" else "openai-chat"
        save_provider(
            Provider(
                "custom",
                protocol,
                url,
                models=("vp-test-model",),
                default_model="vp-test-model",
            ),
        )
        env, args = prepare_provider(agent, ["custom"], {})
        root = tmp_path / "profile"
        roots = {
            "pi": root / ".pi/agent",
            "codex": root / ".codex",
            "qwen": root / ".qwen",
            "tau": root / ".tau",
            "jcode": root / ".jcode",
            "opencode": root / ".config/opencode",
        }
        native = roots[agent]
        native.mkdir(parents=True)
        originals = {
            "pi": {
                "auth.json": '{"custom":{"type":"api_key","key":"stale-key"}}',
                "settings.json": "{}",
            },
            "codex": {
                "auth.json": '{"OPENAI_API_KEY":"stale-key"}',
                "config.toml": 'model = "original"\n',
            },
            # Agents normalise their own files on startup regardless of providers:
            # Qwen Code stamps `$version`, opencode inserts `$schema`. Seed the
            # normalised form so byte-equality only catches provider writes.
            "qwen": {"settings.json": '{\n  "$version": 4\n}'},
            "tau": {"credentials.json": '{"custom":{"type":"api_key","key":"stale-key"}}'},
            # `display` is a table in jcode's config schema.
            "jcode": {
                "config.toml": '[display]\ndiff_mode = "inline"\n',
                "auth.json": '{"keep":"me"}',
            },
            "opencode": {
                "opencode.json": (
                    '{\n  "$schema": "https://opencode.ai/config.json",\n  "theme": "dark"\n}'
                ),
            },
        }[agent]
        originals_content = dict(originals)
        for name, content in originals.items():
            (native / name).write_text(content)
        prompt = "Reply with a greeting."
        if agent == "pi":
            command = [
                "pi",
                *args,
                "--offline",
                "--no-extensions",
                "--no-skills",
                "-p",
                prompt,
            ]
        elif agent == "codex":
            command = [
                "codex",
                *args,
                "exec",
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
                prompt,
            ]
        elif agent == "qwen":
            command = ["qwen", *args, "-p", prompt]
        elif agent == "tau":
            command = ["tau", *args, "-p", prompt]
        elif agent == "jcode":
            command = ["jcode", *args, "run", prompt]
        else:
            # A positional prompt would start the TUI; `run` is the one-shot mode.
            command = ["opencode", *args, "run", prompt]
        argv, wrapper_env = wrap_provider_command(agent, command)
        environment = {
            "HOME": "/config",
            "USER_UID": str(os.getuid()),
            "USER_GID": str(os.getgid()),
            **env,
            **wrapper_env,
        }
        if agent == "pi":
            environment["PI_CODING_AGENT_DIR"] = "/config/.pi/agent"
            environment["PI_OFFLINE"] = "1"
        volumes = {str(root): {"bind": "/config", "mode": "rw"}}
        if agent in WRAPPED_AGENTS:
            # Environment-routed agents (qwen) need no bootstrap mount.
            host_script, mount, mode = bootstrap_volume(agent)
            volumes[host_script] = {"bind": mount, "mode": mode}
        if agent == "qwen":
            # The qwen entrypoint symlinks ~/.qwen to QWEN_CONFIG_DIR (VibePod mounts /qwen).
            environment["QWEN_CONFIG_DIR"] = "/qwen"
            volumes[str(native)] = {"bind": "/qwen", "mode": "rw"}
        if agent == "opencode":
            environment["XDG_CONFIG_HOME"] = "/config/.config"
            environment["XDG_DATA_HOME"] = "/config/.local/share"
        container = client.containers.run(
            f"vp-provider-test-{agent}",
            command=argv,
            detach=True,
            environment=environment,
            volumes=volumes,
            extra_hosts={"host.docker.internal": "host-gateway"},
        )
        status = container.wait(timeout=120)
        logs = container.logs().decode(errors="replace")
        assert status["StatusCode"] == 0, logs
        assert "provider-smoke-ok" in logs
        assert calls and all(body["model"] == "vp-test-model" for _, body, _ in calls)
        assert all("stale-key" not in (auth or "") for _, _, auth in calls)
        for name, content in originals_content.items():
            assert (native / name).read_text() == content
        # Session persistence, where the layout is known.
        session_globs = {
            "pi": (native / "sessions", "*.jsonl"),
            "codex": (native / "sessions", "*.jsonl"),
            "tau": (native / "sessions", "*.jsonl"),
            "opencode": (root / ".local/share/opencode", "*"),
        }
        if agent in session_globs:
            directory, pattern = session_globs[agent]
            assert list(directory.rglob(pattern)), f"Session data must persist under {directory}"
        if agent == "pi":
            assert not (native / "models.json").exists()
    finally:
        if container is not None:
            container.remove(force=True)
        client.close()
        server.shutdown()
        server.server_close()
        thread.join()
