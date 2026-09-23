"""Pi and Codex provider plans and container-side temporary config behavior."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from vibepod.core.provider_launch import prepare_provider
from vibepod.core.providers import Provider, save_provider

# The provider store enforces POSIX owner-only permissions and refuses other platforms.
pytestmark = pytest.mark.skipif(os.name == "nt", reason="provider store is POSIX-only")


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("VP_PROVIDERS_DIR", str(tmp_path / "providers"))
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path / "config"))
    return tmp_path


def test_wrapper_argv_is_shell_safe_and_carries_command_in_env():
    from vibepod.core.provider_runtime import BOOTSTRAP_MOUNT, COMMAND_ENV, wrap_provider_command

    real = ["codex", "-c", 'model_providers.vibepod={name="x"}', "exec", "hi there $(x)"]
    argv, env = wrap_provider_command("codex", real)
    assert argv == ["node", BOOTSTRAP_MOUNT]
    # An image entrypoint using `sh -c "$*"` re-parses argv as shell source, so
    # every element must survive word splitting and quote removal unchanged.
    assert all(re.fullmatch(r"[A-Za-z0-9_./-]+", arg) for arg in argv)
    assert json.loads(env[COMMAND_ENV]) == real
    assert wrap_provider_command("claude", real) == (real, {})


def test_bootstrap_script_installed_once_under_config_root(registry):
    from vibepod.core.provider_runtime import bootstrap_volume

    host, mount, mode = bootstrap_volume("pi")
    assert Path(host).is_file()
    assert Path(host).is_relative_to(registry / "config")
    assert mode == "ro"
    first = Path(host).stat().st_mtime_ns
    assert bootstrap_volume("pi") == (host, mount, mode)
    assert Path(host).stat().st_mtime_ns == first


@pytest.mark.parametrize(
    "protocol,api",
    [
        ("openai-chat", "openai-completions"),
        ("openai-responses", "openai-responses"),
        ("anthropic", "anthropic-messages"),
    ],
)
def test_pi_protocols_and_models(registry, protocol, api):
    save_provider(
        Provider(
            "llmapi",
            protocol,
            "https://example.com" if protocol == "anthropic" else "https://example.com/v1",
            auth="key",
            models=("a", "b"),
            default_model="b",
        ),
        key="secret",
    )
    env, args = prepare_provider("pi", ["llmapi"], {})
    plan = json.loads(env["VIBEPOD_PROVIDER_PLAN"])
    assert plan["providers"]["llmapi"]["api"] == api
    assert plan["providers"]["llmapi"]["models"] == [{"id": "a"}, {"id": "b"}]
    assert "secret" not in env["VIBEPOD_PROVIDER_PLAN"]
    assert env["VIBEPOD_PROVIDER_KEY_0"] == "secret"
    assert args == ["--provider", "llmapi", "--model", "b"]


def test_pi_multiple_providers_do_not_choose_arbitrary_default(registry):
    for name in ("one", "two"):
        save_provider(
            Provider(name, "openai-chat", "http://localhost/v1", models=("a",), default_model="a"),
        )
    env, args = prepare_provider("pi", ["one", "two"], {})
    assert set(json.loads(env["VIBEPOD_PROVIDER_PLAN"])["providers"]) == {"one", "two"}
    assert args == []


def test_codex_uses_responses_not_oss(registry):
    save_provider(
        Provider(
            "llmapi",
            "openai-responses",
            "https://example.com/v1",
            auth="key",
            models=("a",),
            default_model="a",
        ),
        key="secret",
    )
    env, args = prepare_provider("codex", ["llmapi"], {})
    assert "responses" in " ".join(args)
    assert "--oss" not in args
    assert "secret" not in " ".join(args)
    assert env["VIBEPOD_PROVIDER_KEY_0"] == "secret"
    assert args[-2:] == ["--model", "a"]


@pytest.mark.parametrize("protocol", ["openai-chat", "anthropic"])
def test_codex_rejects_other_protocols(registry, protocol):
    save_provider(
        Provider("llmapi", protocol, "https://example.com", models=("a",), default_model="a"),
    )
    with pytest.raises(ValueError, match="Responses"):
        prepare_provider("codex", ["llmapi"], {})


@pytest.mark.parametrize(
    "agent,conflict",
    [("pi", "PI_CODING_AGENT_DIR"), ("codex", "CODEX_HOME"), ("pi", "VIBEPOD_PROVIDER_KEY_0")],
)
def test_adapter_reserved_environment_rejected(registry, agent, conflict):
    with pytest.raises(ValueError, match="conflict"):
        prepare_provider(agent, ["unused"], {conflict: "private-value"})


@pytest.mark.skipif(not shutil.which("pi") or os.name == "nt", reason="Installed Pi required")
def test_native_pi_loads_generated_model_catalog(registry):
    from vibepod.core.provider_runtime import bootstrap_volume, wrap_provider_command

    save_provider(
        Provider("vp-smoke", "openai-chat", "http://localhost/v1", models=("vp-test-model",)),
    )
    env, _ = prepare_provider("pi", ["vp-smoke"], {})
    _, wrapper_env = wrap_provider_command(
        "pi",
        ["pi", "--offline", "--no-extensions", "--no-skills", "--list-models", "vp-test-model"],
    )
    result = subprocess.run(
        ["node", bootstrap_volume("pi")[0]],
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(registry),
            "PI_OFFLINE": "1",
            **env,
            **wrapper_env,
        },
        cwd=registry,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "vp-test-model" in result.stdout
    assert "vp-smoke" in result.stdout
    assert not (registry / ".pi/agent/models.json").exists()


@pytest.mark.skipif(
    not shutil.which("node") or os.name == "nt",
    reason="Node and POSIX symlinks required",
)
def test_concurrent_pi_launches_keep_native_catalog_and_relative_resources(registry):
    from concurrent.futures import ThreadPoolExecutor

    save_provider(Provider("custom", "openai-chat", "http://localhost/v1", models=("a",)))
    env, _ = prepare_provider("pi", ["custom"], {})
    native = registry / ".pi/agent"
    native.mkdir(parents=True)
    original = '{"providers":{"existing":{"models":[{"id":"native-model"}]}}}'
    (native / "models.json").write_text(original)
    (native / "settings.json").write_text('{"extensions":["../extension.js"],"theme":"dark"}')
    child = """
const fs = require('fs'), path = require('path');
const dir = process.env.PI_CODING_AGENT_DIR;
const models = JSON.parse(fs.readFileSync(path.join(dir, 'models.json')));
if (!models.providers.existing || !models.providers.custom) process.exit(9);
const settings = JSON.parse(fs.readFileSync(path.join(dir, 'settings.json')));
if (!path.isAbsolute(settings.extensions[0]) || settings.theme !== 'dark') process.exit(8);
console.log(dir);
setTimeout(() => process.exit(0), 100);
"""
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run_bootstrap(env, child, registry), range(2)))
    assert all(r.returncode == 0 for r in results), [r.stderr for r in results]
    assert results[0].stdout != results[1].stdout
    assert all(not Path(r.stdout.strip()).exists() for r in results)
    assert (native / "models.json").read_text() == original


def run_bootstrap(env, child, root, agent="pi", legacy_entrypoint=False):
    from vibepod.core.provider_runtime import bootstrap_volume, wrap_provider_command

    native = root / (".pi/agent" if agent == "pi" else ".codex")
    argv, wrapper_env = wrap_provider_command(agent, ["node", "-e", child])
    argv = ["node", bootstrap_volume("pi")[0]] + argv[2:]
    if legacy_entrypoint:
        # Older images run `sh -c "$*"`: argv is joined and re-parsed as shell.
        argv = ["sh", "-c", " ".join(argv)]
    env = {
        **os.environ,
        **env,
        **wrapper_env,
        "HOME": str(root),
        "PI_CODING_AGENT_DIR": str(native),
    }
    return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=20)


@pytest.mark.skipif(
    not shutil.which("node") or os.name == "nt",
    reason="Node and POSIX symlinks required",
)
@pytest.mark.parametrize("legacy_entrypoint", [False, True])
@pytest.mark.parametrize("agent", ["pi", "codex"])
def test_bootstrap_preserves_profile_and_sessions(registry, agent, legacy_entrypoint):
    protocol = "openai-chat" if agent == "pi" else "openai-responses"
    save_provider(
        Provider("custom", protocol, "http://localhost/v1", models=("a",), default_model="a"),
    )
    env, _ = prepare_provider(agent, ["custom"], {})
    native = registry / (".pi/agent" if agent == "pi" else ".codex")
    native.mkdir(parents=True)
    filename = "settings.json" if agent == "pi" else "config.toml"
    original = '{"theme":"dark"}' if agent == "pi" else 'model = "original"\n'
    (native / filename).write_text(original)
    (native / "auth.json").write_text('{"custom":{"type":"api_key","key":"old-key"}}')
    (native / "auth.json.lock").mkdir()
    child = f"""
const fs = require('fs'), path = require('path');
const dir = process.env.{"PI_CODING_AGENT_DIR" if agent == "pi" else "CODEX_HOME"};
console.log(dir);
if (fs.existsSync(path.join(dir, 'auth.json.lock'))) process.exit(7);
if (process.env.VIBEPOD_PROVIDER_COMMAND || process.env.VIBEPOD_PROVIDER_PLAN) process.exit(6);
fs.writeFileSync(path.join(dir, {json.dumps(filename)}), 'changed');
fs.writeFileSync(path.join(dir, 'sessions', 'saved-session'), 'history');
if ({json.dumps(agent)} === 'pi') {{
  const m = JSON.parse(fs.readFileSync(path.join(dir, 'models.json')));
  if (m.providers.custom.api !== 'openai-completions') process.exit(9);
  const a = JSON.parse(fs.readFileSync(path.join(dir, 'auth.json')));
  if (a.custom.key !== '$VIBEPOD_PROVIDER_KEY_0') process.exit(8);
}}
"""
    result = run_bootstrap(env, child, registry, agent, legacy_entrypoint=legacy_entrypoint)
    assert result.returncode == 0, result.stderr
    assert (native / filename).read_text() == original
    assert "old-key" in (native / "auth.json").read_text()
    assert (native / "sessions/saved-session").read_text() == "history"
    assert not Path(result.stdout.strip()).exists(), "Temporary config should be removed"


@pytest.mark.skipif(
    not shutil.which("node") or os.name == "nt",
    reason="Node and POSIX symlinks required",
)
def test_malformed_native_config_fails_closed(registry):
    save_provider(Provider("custom", "openai-chat", "http://localhost/v1", models=("a",)))
    env, _ = prepare_provider("pi", ["custom"], {})
    native = registry / ".pi/agent"
    native.mkdir(parents=True)
    (native / "models.json").write_text("broken-sensitive-content")
    result = run_bootstrap(env, "process.exit(0)", registry)
    assert result.returncode != 0
    assert "broken-sensitive-content" not in result.stderr
    assert (native / "models.json").read_text() == "broken-sensitive-content"


NEW_PLAN_AGENTS = ["opencode", "tau", "jcode"]


def _save_generic_pair(registry, second_protocol="anthropic"):
    save_provider(
        Provider(
            "hosted",
            "openai-chat",
            "https://example.com/v1",
            auth="key",
            models=("a", "b"),
            default_model="b",
        ),
        key="secret",
    )
    url = (
        "http://localhost:11434" if second_protocol == "anthropic" else "http://localhost:11434/v1"
    )
    save_provider(Provider("local", second_protocol, url, models=("c",), default_model="c"))


@pytest.mark.parametrize("agent", ["opencode", "tau"])
def test_plan_agents_emit_generic_provider_catalog(registry, agent):
    _save_generic_pair(registry)
    env, args = prepare_provider(agent, ["hosted", "local"], {})
    plan = json.loads(env["VIBEPOD_PROVIDER_PLAN"])
    assert plan["agent"] == agent
    assert plan["providers"]["hosted"] == {
        "protocol": "openai-chat",
        "baseUrl": "https://example.com/v1",
        "apiKeyEnv": "VIBEPOD_PROVIDER_KEY_0",
        "authenticated": True,
        "models": ["a", "b"],
    }
    assert plan["providers"]["local"] == {
        "protocol": "anthropic",
        "baseUrl": "http://localhost:11434",
        "apiKeyEnv": "VIBEPOD_PROVIDER_KEY_1",
        "authenticated": False,
        "models": ["c"],
    }
    assert "secret" not in env["VIBEPOD_PROVIDER_PLAN"]
    assert env["VIBEPOD_PROVIDER_KEY_0"] == "secret"
    assert env["VIBEPOD_PROVIDER_KEY_1"] == "vibepod-local"
    assert args == []


@pytest.mark.parametrize("protocol", ["anthropic", "openai-responses"])
def test_jcode_plan_is_chat_completions_only(registry, protocol):
    url = "https://example.com" if protocol == "anthropic" else "https://example.com/v1"
    save_provider(Provider("other", protocol, url, models=("m",), default_model="m"))
    with pytest.raises(ValueError, match="Chat Completions"):
        prepare_provider("jcode", ["other"], {})


@pytest.mark.parametrize(
    "agent,expected",
    [
        ("tau", ["--provider", "hosted", "--model", "b"]),
        ("jcode", ["--provider-profile", "hosted", "--model", "b"]),
        ("opencode", []),
    ],
)
def test_single_provider_default_uses_native_selection_flags(registry, agent, expected):
    _save_generic_pair(registry, second_protocol="openai-chat")
    env, args = prepare_provider(agent, ["hosted"], {})
    assert json.loads(env["VIBEPOD_PROVIDER_PLAN"])["providers"]["hosted"]["defaultModel"] == "b"
    assert args == expected
    _, args = prepare_provider(agent, ["hosted", "local"], {})
    assert args == [], "several providers never pick a default"


@pytest.mark.parametrize("agent", NEW_PLAN_AGENTS)
def test_plan_agents_without_default_pass_no_selection(registry, agent):
    save_provider(Provider("nodefault", "openai-chat", "http://localhost/v1", models=("a", "b")))
    env, args = prepare_provider(agent, ["nodefault"], {})
    assert args == []
    assert "defaultModel" not in json.loads(env["VIBEPOD_PROVIDER_PLAN"])["providers"]["nodefault"]


@pytest.mark.parametrize("agent", NEW_PLAN_AGENTS)
def test_plan_agents_require_selected_models_and_distinct_names(registry, agent):
    save_provider(Provider("empty", "openai-chat", "http://localhost/v1"))
    with pytest.raises(ValueError, match="no selected models"):
        prepare_provider(agent, ["empty"], {})
    with pytest.raises(ValueError, match="distinct"):
        prepare_provider(agent, ["x", "x"], {})


@pytest.mark.parametrize(
    "agent,conflict",
    [
        ("tau", "TAU_HOME"),
        ("jcode", "JCODE_HOME"),
        ("jcode", "JCODE_CONFIG_DIR"),
        ("opencode", "OPENCODE_CONFIG_DIR"),
        ("opencode", "VIBEPOD_PROVIDER_KEY_0"),
    ],
)
def test_plan_agents_reserved_environment(registry, agent, conflict):
    with pytest.raises(ValueError, match="conflict"):
        prepare_provider(agent, ["unused"], {conflict: "private-value"})


@pytest.mark.parametrize("agent", NEW_PLAN_AGENTS)
def test_plan_agents_authenticated_http_rejected(registry, agent):
    save_provider(
        Provider(
            "insecure",
            "openai-chat",
            "http://example.com/v1",
            auth="key",
            models=("m",),
            default_model="m",
        ),
        key="secret",
    )
    with pytest.raises(ValueError, match="HTTPS"):
        prepare_provider(agent, ["insecure"], {})


def _hosted(registry):
    save_provider(
        Provider(
            "hosted",
            "openai-chat",
            "https://example.com/v1",
            auth="key",
            models=("a", "b"),
            default_model="b",
        ),
        key="secret",
    )


def run_opencode_bootstrap(env, child, registry, config_dir=None):
    from vibepod.core.provider_runtime import bootstrap_volume, wrap_provider_command

    argv, wrapper_env = wrap_provider_command("opencode", ["node", "-e", child])
    argv = ["node", bootstrap_volume("opencode")[0]] + argv[2:]
    launch_env = {
        **os.environ,
        **env,
        **wrapper_env,
        "XDG_CONFIG_HOME": str(registry / ".config"),
        "HOME": str(registry),
    }
    launch_env.pop("OPENCODE_CONFIG_DIR", None)
    if config_dir is not None:
        launch_env["OPENCODE_CONFIG_DIR"] = str(config_dir)
    return subprocess.run(argv, env=launch_env, capture_output=True, text=True, timeout=20)


OPENCODE_CHILD = """
const fs = require('fs'), path = require('path');
const view = process.env.OPENCODE_CONFIG_DIR;
if (!view || view === process.env.VIBEPOD_REAL_CONFIG_DIR) process.exit(10);
if (process.env.XDG_CONFIG_HOME !== process.env.VIBEPOD_XDG) process.exit(11);
const config = JSON.parse(fs.readFileSync(path.join(view, 'opencode.json')));
if (config.theme !== 'dark') process.exit(9);
const provider = config.provider.hosted;
if (provider.npm !== '@ai-sdk/openai-compatible') process.exit(8);
if (provider.options.baseURL !== 'https://example.com/v1') process.exit(8);
if (provider.options.apiKey !== '{env:VIBEPOD_PROVIDER_KEY_0}') process.exit(7);
if (!provider.models.a || !provider.models.b) process.exit(7);
if (config.model !== 'hosted/b') process.exit(6);
const realAgents = fs.realpathSync(path.join(process.env.VIBEPOD_REAL_CONFIG_DIR, 'agents'));
if (fs.realpathSync(path.join(view, 'agents')) !== realAgents) process.exit(5);
if (process.env.VIBEPOD_PROVIDER_COMMAND || process.env.VIBEPOD_PROVIDER_PLAN) process.exit(4);
fs.writeFileSync(path.join(view, 'opencode.json'), 'changed');
fs.writeFileSync(path.join(view, 'agents', 'saved.md'), 'kept');
console.log(view);
setTimeout(() => process.exit(0), 100);
"""


@pytest.mark.skipif(
    not shutil.which("node") or os.name == "nt",
    reason="Node and POSIX symlinks required",
)
@pytest.mark.parametrize("explicit_dir", [True, False])
def test_bootstrap_opencode_view_becomes_config_dir(registry, explicit_dir):
    _hosted(registry)
    env, _ = prepare_provider("opencode", ["hosted"], {})
    xdg = registry / ".config"
    # VibePod points OPENCODE_CONFIG_DIR at the config root; without it the
    # global $XDG_CONFIG_HOME/opencode directory is the base of the view.
    real = registry / "cfg" if explicit_dir else xdg / "opencode"
    real.mkdir(parents=True)
    (real / "opencode.json").write_text('{"theme":"dark"}')
    (real / "agents").mkdir()
    (xdg / "git").mkdir(parents=True)
    result = run_opencode_bootstrap(
        {**env, "VIBEPOD_REAL_CONFIG_DIR": str(real), "VIBEPOD_XDG": str(xdg)},
        OPENCODE_CHILD,
        registry,
        config_dir=real if explicit_dir else None,
    )
    assert result.returncode == 0, result.stderr
    assert (real / "opencode.json").read_text() == '{"theme":"dark"}'
    assert (real / "agents/saved.md").read_text() == "kept"
    assert not Path(result.stdout.strip()).exists()


@pytest.mark.skipif(
    not shutil.which("node") or os.name == "nt",
    reason="Node and POSIX symlinks required",
)
def test_bootstrap_opencode_refuses_jsonc_config(registry):
    _hosted(registry)
    env, _ = prepare_provider("opencode", ["hosted"], {})
    real = registry / "cfg"
    real.mkdir()
    (real / "opencode.jsonc").write_text('{"model": "secret-model" // c\n}')
    result = run_opencode_bootstrap(env, "process.exit(0)", registry, config_dir=real)
    assert result.returncode != 0
    assert "opencode.jsonc" in result.stderr and "secret-model" not in result.stderr


PYTHON3 = pytest.mark.skipif(
    not shutil.which("python3") or os.name == "nt",
    reason="python3 and POSIX symlinks required",
)

# A complete Tau user-catalog entry (tau_coding.catalog_loader._CatalogProvider, extra=forbid).
TAU_USER_CATALOG = """schema_version = 1

[[providers]]
name = "mine"
display_name = "Mine"
kind = "openai-compatible"
base_url = "https://mine.example/v1"
api_key_env = "MINE_KEY"
models = ["m1"]
default_model = "m1"
docs_url = "https://mine.example/docs"

[providers.context_windows]
m1 = 8192
"""


def run_python_bootstrap(env, script, root, agent, extra_env=None):
    from vibepod.core.provider_runtime import bootstrap_volume, wrap_provider_command

    argv, wrapper_env = wrap_provider_command(agent, ["python3", "-c", script])
    argv = ["python3", bootstrap_volume(agent)[0]] + argv[2:]
    return subprocess.run(
        argv,
        env={
            **os.environ,
            **env,
            **wrapper_env,
            "HOME": str(root),
            "VIBEPOD_TEST_HOME": str(root),
            **(extra_env or {}),
        },
        capture_output=True,
        text=True,
        timeout=20,
    )


@PYTHON3
def test_tau_private_view_appends_catalog_and_copies_settings(registry):
    _hosted(registry)
    env, _ = prepare_provider("tau", ["hosted"], {})
    native = registry / ".tau"
    native.mkdir(parents=True)
    (native / "catalog.toml").write_text(TAU_USER_CATALOG)
    providers_json = '{"default_provider":"mine","provider_preferences":{}}'
    credentials_json = '{"mine":{"type":"api_key","key":"stale-key"}}'
    (native / "providers.json").write_text(providers_json)
    (native / "credentials.json").write_text(credentials_json)
    (native / "sessions").mkdir()
    script = """
import json, os
try:
    import tomllib
except ImportError:  # Python 3.10 runners
    import tomli as tomllib
home, real = os.environ['HOME'], os.environ['VIBEPOD_TEST_HOME']
assert home != real
catalog = tomllib.loads(open(home + '/.tau/catalog.toml').read())
assert catalog['schema_version'] == 1
assert [p['name'] for p in catalog['providers']] == ['mine', 'hosted'], catalog
mine = catalog['providers'][0]
assert mine['context_windows'] == {'m1': 8192}, mine
hosted = catalog['providers'][1]
assert hosted == {
    'name': 'hosted', 'display_name': 'hosted', 'kind': 'openai-compatible',
    'api': 'openai-completions', 'base_url': 'https://example.com/v1',
    'api_key_env': 'VIBEPOD_PROVIDER_KEY_0', 'models': ['a', 'b'],
    'default_model': 'b', 'docs_url': 'https://example.com/v1',
}, hosted
for name in ('providers.json', 'credentials.json'):
    assert not os.path.islink(home + '/.tau/' + name), name
assert json.load(open(home + '/.tau/providers.json'))['default_provider'] == 'mine'
assert os.path.realpath(home + '/.tau/sessions') == real + '/.tau/sessions'
assert not (os.environ.get('VIBEPOD_PROVIDER_PLAN') or os.environ.get('VIBEPOD_PROVIDER_COMMAND'))
assert os.environ['VIBEPOD_PROVIDER_KEY_0'] == 'secret'
open(home + '/.tau/sessions/saved', 'w').write('history')
for name in ('providers.json', 'credentials.json', 'catalog.toml'):
    open(home + '/.tau/' + name, 'w').write('mutated')
"""
    result = run_python_bootstrap(env, script, registry, "tau")
    assert result.returncode == 0, result.stderr
    assert (native / "catalog.toml").read_text() == TAU_USER_CATALOG
    assert (native / "providers.json").read_text() == providers_json
    assert (native / "credentials.json").read_text() == credentials_json
    assert (native / "sessions/saved").read_text() == "history"


@PYTHON3
def test_tau_catalog_created_when_profile_has_none(registry):
    save_provider(Provider("local", "anthropic", "http://localhost:11434", models=("c",)))
    env, _ = prepare_provider("tau", ["local"], {})
    script = """
import os
try:
    import tomllib
except ImportError:  # Python 3.10 runners
    import tomli as tomllib
catalog = tomllib.loads(open(os.environ['HOME'] + '/.tau/catalog.toml').read())
assert catalog['schema_version'] == 1
[entry] = catalog['providers']
assert entry['kind'] == 'anthropic' and entry['api'] == 'anthropic-messages', entry
assert entry['default_model'] == 'c' and entry['api_key_env'] == 'VIBEPOD_PROVIDER_KEY_0'
assert not os.path.exists(os.environ['HOME'] + '/.tau/providers.json')
"""
    result = run_python_bootstrap(env, script, registry, "tau")
    assert result.returncode == 0, result.stderr
    assert not (registry / ".tau/catalog.toml").exists()


SH = pytest.mark.skipif(
    not (shutil.which("sh") and shutil.which("base64") and shutil.which("python3"))
    or os.name == "nt",
    reason="sh, base64, python3, and POSIX symlinks required",
)


def run_sh_bootstrap(env, command, root, legacy_entrypoint=False):
    """Run the jcode sh bootstrap around ``command`` with ``root`` as HOME."""
    from vibepod.core.provider_runtime import bootstrap_volume, wrap_provider_command

    argv, wrapper_env = wrap_provider_command("jcode", command)
    argv = ["sh", bootstrap_volume("jcode")[0]] + argv[2:]
    if legacy_entrypoint:
        # Older images run `sh -c "$*"`: argv is joined and re-parsed as shell.
        argv = ["sh", "-c", " ".join(argv)]
    return subprocess.run(
        argv,
        env={**os.environ, **env, **wrapper_env, "HOME": str(root), "VIBEPOD_TEST_HOME": str(root)},
        capture_output=True,
        text=True,
        timeout=20,
    )


JCODE_CHECK = """
import json, os
try:
    import tomllib
except ImportError:  # Python 3.10 runners
    import tomli as tomllib
real = os.environ['VIBEPOD_TEST_HOME']
assert os.environ['HOME'] == real, 'HOME must stay the real home for jcode'
view = os.environ['JCODE_HOME']
assert view != real + '/.jcode'
config = tomllib.loads(open(view + '/config.toml').read())
assert config['display'] == {'diff_mode': 'inline'}, config
assert config['providers']['mine']['base_url'] == 'https://mine.example/v1'
hosted = config['providers']['hosted']
assert hosted['type'] == 'openai-compatible' and hosted['auth'] == 'bearer', hosted
assert hosted['base_url'] == 'https://example.com/v1'
assert hosted['api_key_env'] == 'VIBEPOD_PROVIDER_KEY_0' and hosted['default_model'] == 'b'
assert hosted['models'] == [{'id': 'a'}, {'id': 'b'}], hosted
assert os.environ['VIBEPOD_PROVIDER_KEY_0'] == 'secret'
assert json.load(open(view + '/auth.json')) == {'keep': 'me'}
assert os.path.realpath(view + '/config/jcode') == real + '/.config/jcode'
assert open(view + '/config/jcode/provider-mine.env').read() == 'MINE=1'
assert os.path.realpath(view + '/external') == real
assert os.path.realpath(view + '/sessions') == real + '/.jcode/sessions'
for name in ('COMMAND_B64', 'CONFIG_TOML', 'PLAN', 'NAMES'):
    assert 'VIBEPOD_PROVIDER_' + name not in os.environ, name
open(view + '/sessions/saved', 'w').write('history')
open(view + '/config.toml', 'w').write('mutated')
print(view)
"""


@SH
@pytest.mark.parametrize("legacy_entrypoint", [False, True])
def test_jcode_sh_view_appends_profiles_and_links_back(registry, legacy_entrypoint):
    _hosted(registry)
    env, _ = prepare_provider("jcode", ["hosted"], {})
    jcode = registry / ".jcode"
    jcode.mkdir()
    original = (
        '[display]\ndiff_mode = "inline"\n\n[providers.mine]\ntype = "openai-compatible"\n'
        'base_url = "https://mine.example/v1"'  # no trailing newline on purpose
    )
    (jcode / "config.toml").write_text(original)
    (jcode / "auth.json").write_text('{"keep":"me"}')
    (jcode / "sessions").mkdir()
    (registry / ".config/jcode").mkdir(parents=True)
    (registry / ".config/jcode/provider-mine.env").write_text("MINE=1")
    result = run_sh_bootstrap(
        env,
        ["python3", "-c", JCODE_CHECK],
        registry,
        legacy_entrypoint=legacy_entrypoint,
    )
    assert result.returncode == 0, result.stderr
    assert (jcode / "config.toml").read_text() == original
    assert (jcode / "sessions/saved").read_text() == "history"
    assert not Path(result.stdout.strip()).exists(), "view must be removed"


@SH
def test_jcode_sh_creates_config_and_uses_auth_none_without_key(registry):
    save_provider(Provider("local", "openai-chat", "http://localhost:11434/v1", models=("c",)))
    env, _ = prepare_provider("jcode", ["local"], {})
    script = """
import os
try:
    import tomllib
except ImportError:  # Python 3.10 runners
    import tomli as tomllib
config = tomllib.loads(open(os.environ['JCODE_HOME'] + '/config.toml').read())
local = config['providers']['local']
assert local['auth'] == 'none', local
assert 'api_key_env' not in local and 'default_model' not in local, local
assert local['models'] == [{'id': 'c'}]
"""
    result = run_sh_bootstrap(env, ["python3", "-c", script], registry)
    assert result.returncode == 0, result.stderr
    assert not (registry / ".jcode/config.toml").exists()


@SH
def test_jcode_sh_preserves_every_argument(registry):
    save_provider(Provider("local", "openai-chat", "http://localhost/v1", models=("c",)))
    env, _ = prepare_provider("jcode", ["local"], {})
    tricky = ["a b", "", 'q"uote', "$(x) `y` {z}", "trailing\n", "ünï", "--model=m"]
    script = "import json, sys; print(json.dumps(sys.argv[1:]))"
    result = run_sh_bootstrap(env, ["python3", "-c", script, *tricky], registry)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == tricky


@SH
def test_jcode_sh_forwards_termination_and_cleans_up(registry):
    from vibepod.core.provider_runtime import bootstrap_volume, wrap_provider_command

    save_provider(Provider("local", "openai-chat", "http://localhost/v1", models=("c",)))
    env, _ = prepare_provider("jcode", ["local"], {})
    marker = registry / "view-path"
    script = f"""
import os, time
open({str(marker)!r}, 'w').write(os.environ['JCODE_HOME'])
time.sleep(30)
"""
    argv, wrapper_env = wrap_provider_command("jcode", ["python3", "-c", script])
    process = subprocess.Popen(
        ["sh", bootstrap_volume("jcode")[0]],
        env={**os.environ, **env, **wrapper_env, "HOME": str(registry)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    import signal
    import time

    for _ in range(100):
        if marker.exists():
            break
        time.sleep(0.05)
    assert marker.exists(), process.stderr.read()
    process.send_signal(signal.SIGTERM)
    status = process.wait(timeout=10)
    assert status == 143, process.stderr.read()
    assert not Path(marker.read_text()).exists(), "view must be removed after termination"


@SH
def test_jcode_sh_rejects_native_name_clash(registry):
    _hosted(registry)
    env, _ = prepare_provider("jcode", ["hosted"], {})
    (registry / ".jcode").mkdir()
    (registry / ".jcode/config.toml").write_text('[providers.hosted]\nbase_url = "x"\n')
    result = run_sh_bootstrap(env, ["python3", "-c", "pass"], registry)
    assert result.returncode != 0
    assert "hosted" in result.stderr and "secret" not in result.stderr


def test_jcode_sections_rendered_on_host_escape_toml(registry):
    from vibepod.core.provider_adapters import JCODE_CONFIG_ENV, JCODE_NAMES_ENV

    save_provider(
        Provider(
            "custom",
            "openai-chat",
            "https://example.com/v1",
            auth="key",
            models=('we"ird', "back\\slash"),
            default_model='we"ird',
        ),
        key="secret",
    )
    env, _ = prepare_provider("jcode", ["custom"], {})
    import sys

    if sys.version_info < (3, 11):
        pytest.skip("tomllib required")
    import tomllib

    rendered = tomllib.loads(env[JCODE_CONFIG_ENV])
    custom = rendered["providers"]["custom"]
    assert custom["models"] == [{"id": 'we"ird'}, {"id": "back\\slash"}]
    assert custom["default_model"] == 'we"ird'
    assert "secret" not in env[JCODE_CONFIG_ENV]
    assert env[JCODE_NAMES_ENV] == "custom"


@PYTHON3
def test_python_bootstrap_rejects_native_name_clash(registry):
    _hosted(registry)
    env, _ = prepare_provider("tau", ["hosted"], {})
    (registry / ".tau").mkdir()
    (registry / ".tau/catalog.toml").write_text(TAU_USER_CATALOG.replace('"mine"', '"hosted"'))
    result = run_python_bootstrap(env, "pass", registry, "tau")
    assert result.returncode != 0
    assert "hosted" in result.stderr and "secret" not in result.stderr


@PYTHON3
@pytest.mark.parametrize(
    "agent,filename",
    [("tau", ".tau/catalog.toml"), ("jcode", ".jcode/config.toml")],
)
def test_python_bootstrap_fails_closed_on_malformed_config(registry, agent, filename):
    save_provider(Provider("custom", "openai-chat", "http://localhost/v1", models=("a",)))
    env, _ = prepare_provider(agent, ["custom"], {})
    (registry / filename).parent.mkdir(parents=True)
    (registry / filename).write_text("broken-sensitive-content = [")
    result = run_python_bootstrap(env, "pass", registry, agent)
    assert result.returncode != 0
    assert "broken-sensitive-content" not in result.stderr
    assert (registry / filename).read_text() == "broken-sensitive-content = ["


@PYTHON3
def test_tau_view_keeps_rest_of_home_visible(registry):
    save_provider(Provider("custom", "openai-chat", "http://localhost/v1", models=("a",)))
    env, _ = prepare_provider("tau", ["custom"], {})
    (registry / ".gitconfig").write_text("[user]\n\tname = me\n")
    (registry / ".ssh").mkdir()
    (registry / ".ssh/id_test").write_text("key")
    (registry / ".agents/skills").mkdir(parents=True)
    script = """
import os
home, real = os.environ['HOME'], os.environ['VIBEPOD_TEST_HOME']
for name in ('.gitconfig', '.ssh/id_test', '.agents/skills'):
    assert os.path.realpath(os.path.join(home, name)) == os.path.join(real, name), name
open(os.path.join(home, '.cache-marker'), 'w').write('view only')
"""
    result = run_python_bootstrap(env, script, registry, "tau")
    assert result.returncode == 0, result.stderr
    assert not (registry / ".cache-marker").exists()


@PYTHON3
def test_python_bootstrap_escapes_toml_strings(registry):
    save_provider(
        Provider(
            "custom",
            "openai-chat",
            "https://example.com/v1",
            models=('we"ird', "back\\slash", "plain"),
        ),
    )
    env, _ = prepare_provider("tau", ["custom"], {})
    script = """
import os
try:
    import tomllib
except ImportError:  # Python 3.10 runners
    import tomli as tomllib
[entry] = tomllib.loads(open(os.environ['HOME'] + '/.tau/catalog.toml').read())['providers']
assert entry['models'] == ['we"ird', 'back\\\\slash', 'plain'], entry
assert entry['base_url'] == 'https://example.com/v1'
"""
    result = run_python_bootstrap(env, script, registry, "tau")
    assert result.returncode == 0, result.stderr


@PYTHON3
def test_python_bootstrap_reports_signal_death_like_a_shell(registry):
    save_provider(Provider("custom", "openai-chat", "http://localhost/v1", models=("a",)))
    env, _ = prepare_provider("tau", ["custom"], {})
    result = run_python_bootstrap(
        env,
        "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
        registry,
        "tau",
    )
    assert result.returncode == 143


def test_opencode_inline_config_env_is_reserved(registry):
    with pytest.raises(ValueError, match="OPENCODE_CONFIG_CONTENT"):
        prepare_provider("opencode", ["unused"], {"OPENCODE_CONFIG_CONTENT": "{}"})


def _save_with_settings(registry, protocol="openai-chat", url="https://example.com/v1"):
    from vibepod.core.providers import ModelSettings

    save_provider(
        Provider(
            "tuned",
            protocol,
            url,
            models=("big", "plain"),
            default_model="big",
            model_settings={
                "big": ModelSettings(
                    context_window=32768,
                    max_output_tokens=8192,
                    reasoning_levels=("low", "high", "xhigh"),
                    reasoning_default="high",
                ),
                "plain": ModelSettings(reasoning=False),
            },
        ),
    )


def test_pi_receives_limits_thinking_map_and_startup_level(registry):
    _save_with_settings(registry)
    env, args = prepare_provider("pi", ["tuned"], {})
    models = json.loads(env["VIBEPOD_PROVIDER_PLAN"])["providers"]["tuned"]["models"]
    assert models[0] == {
        "id": "big",
        "contextWindow": 32768,
        "maxTokens": 8192,
        "reasoning": True,
        # Unsupported levels are null; supported standard levels keep Pi's default
        # mapping (omitted); xhigh needs an explicit value.
        "thinkingLevelMap": {
            "off": None,
            "minimal": None,
            "medium": None,
            "xhigh": "xhigh",
            "max": None,
        },
    }
    assert models[1] == {"id": "plain", "reasoning": False}
    assert args == ["--provider", "tuned", "--model", "big", "--thinking", "high"]


def test_pi_anthropic_provider_never_maps_xhigh(registry):
    _save_with_settings(registry, protocol="anthropic", url="https://example.com")
    env, _ = prepare_provider("pi", ["tuned"], {})
    models = json.loads(env["VIBEPOD_PROVIDER_PLAN"])["providers"]["tuned"]["models"]
    assert models[0]["thinkingLevelMap"]["xhigh"] is None


def test_codex_receives_context_window_and_reasoning_effort(registry):
    _save_with_settings(registry, protocol="openai-responses")
    _, args = prepare_provider("codex", ["tuned"], {})
    joined = " ".join(args)
    assert "model_context_window=32768" in joined
    assert 'model_reasoning_effort="high"' in joined
    assert args[-2:] == ["--model", "big"]


def test_codex_off_level_becomes_none(registry):
    from vibepod.core.providers import ModelSettings

    save_provider(
        Provider(
            "quiet",
            "openai-responses",
            "https://example.com/v1",
            models=("m",),
            default_model="m",
            model_settings={"m": ModelSettings(reasoning_default="off")},
        ),
    )
    _, args = prepare_provider("codex", ["quiet"], {})
    assert 'model_reasoning_effort="none"' in " ".join(args)


@pytest.mark.parametrize("agent", ["opencode", "tau", "jcode"])
def test_generic_plan_carries_settings(registry, agent):
    _save_with_settings(registry)
    env, _ = prepare_provider(agent, ["tuned"], {})
    entry = json.loads(env["VIBEPOD_PROVIDER_PLAN"])["providers"]["tuned"]
    assert entry["settings"] == {
        "big": {
            "contextWindow": 32768,
            "maxOutputTokens": 8192,
            "reasoning": True,
            "reasoningLevels": ["low", "high", "xhigh"],
            "reasoningDefault": "high",
        },
        "plain": {"reasoning": False},
    }
    if agent == "jcode":
        import sys

        if sys.version_info >= (3, 11):
            import tomllib

            from vibepod.core.provider_adapters import JCODE_CONFIG_ENV

            rendered = tomllib.loads(env[JCODE_CONFIG_ENV])
            assert rendered["providers"]["tuned"]["models"] == [
                {"id": "big", "context_window": 32768},
                {"id": "plain"},
            ]


@PYTHON3
def test_tau_catalog_carries_model_metadata_and_thinking(registry):
    _save_with_settings(registry)
    env, _ = prepare_provider("tau", ["tuned"], {})
    script = """
import os
try:
    import tomllib
except ImportError:  # Python 3.10 runners
    import tomli as tomllib
[entry] = tomllib.loads(open(os.environ['HOME'] + '/.tau/catalog.toml').read())['providers']
assert entry['thinking_parameter'] == 'reasoning_effort', entry
assert entry['thinking_default'] == 'high', entry
big = entry['model_metadata']['big']
assert big == {'context_window': 32768, 'max_tokens': 8192, 'reasoning': True,
               'unsupported_thinking_levels': ['off', 'minimal', 'medium']}, big
assert entry['model_metadata']['plain'] == {'reasoning': False}
"""
    result = run_python_bootstrap(env, script, registry, "tau")
    assert result.returncode == 0, result.stderr


@PYTHON3
def test_tau_anthropic_thinking_parameter(registry):
    _save_with_settings(registry, protocol="anthropic", url="https://example.com")
    env, _ = prepare_provider("tau", ["tuned"], {})
    script = """
import os
try:
    import tomllib
except ImportError:  # Python 3.10 runners
    import tomli as tomllib
[entry] = tomllib.loads(open(os.environ['HOME'] + '/.tau/catalog.toml').read())['providers']
assert entry['thinking_parameter'] == 'anthropic.thinking', entry
"""
    result = run_python_bootstrap(env, script, registry, "tau")
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(
    not shutil.which("node") or os.name == "nt",
    reason="Node and POSIX symlinks required",
)
def test_opencode_models_receive_limit_and_reasoning(registry):
    _save_with_settings(registry)
    env, _ = prepare_provider("opencode", ["tuned"], {})
    real = registry / "cfg"
    real.mkdir()
    child = """
const fs = require('fs'), path = require('path');
const file = path.join(process.env.OPENCODE_CONFIG_DIR, 'opencode.json');
const config = JSON.parse(fs.readFileSync(file));
const models = config.provider.tuned.models;
const big = JSON.stringify(models.big), plain = JSON.stringify(models.plain);
const expected = JSON.stringify({ limit: { context: 32768, output: 8192 }, reasoning: true });
if (big !== expected) process.exit(9);
if (plain !== JSON.stringify({ reasoning: false })) process.exit(8);
if (config.model !== 'tuned/big') process.exit(7);
"""
    result = run_opencode_bootstrap(env, child, registry, config_dir=real)
    assert result.returncode == 0, result.stderr


@PYTHON3
def test_tau_view_persists_sessions_created_in_a_fresh_profile(registry):
    save_provider(Provider("custom", "openai-chat", "http://localhost/v1", models=("a",)))
    env, _ = prepare_provider("tau", ["custom"], {})
    assert not (registry / ".tau").exists()
    script = """
import os
home = os.environ['HOME']
os.makedirs(home + '/.tau/sessions/project-x', exist_ok=True)
open(home + '/.tau/sessions/project-x/default.jsonl', 'w').write('{}')
open(home + '/.agents/marker', 'w').write('x')
"""
    result = run_python_bootstrap(env, script, registry, "tau")
    assert result.returncode == 0, result.stderr
    assert (registry / ".tau/sessions/project-x/default.jsonl").read_text() == "{}"
    assert (registry / ".agents/marker").exists()


@SH
def test_jcode_view_persists_sessions_created_in_a_fresh_profile(registry):
    save_provider(Provider("custom", "openai-chat", "http://localhost/v1", models=("a",)))
    env, _ = prepare_provider("jcode", ["custom"], {})
    assert not (registry / ".jcode").exists()
    script = """
import os

view = os.environ['JCODE_HOME']
open(view + '/sessions/saved.json', 'w').write('{}')
open(view + '/logs/run.log', 'w').write('x')
"""
    result = run_sh_bootstrap(env, ["python3", "-c", script], registry)
    assert result.returncode == 0, result.stderr
    assert (registry / ".jcode/sessions/saved.json").read_text() == "{}"
    assert (registry / ".jcode/logs/run.log").exists()


@pytest.mark.skipif(
    not shutil.which("node") or os.name == "nt",
    reason="Node and POSIX symlinks required",
)
def test_opencode_anthropic_provider_gets_the_v1_prefix(registry):
    save_provider(
        Provider("claude-proxy", "anthropic", "https://proxy.example/", models=("m",)),
    )
    env, _ = prepare_provider("opencode", ["claude-proxy"], {})
    real = registry / "cfg"
    real.mkdir()
    child = """
const fs = require('fs'), path = require('path');
const file = path.join(process.env.OPENCODE_CONFIG_DIR, 'opencode.json');
const config = JSON.parse(fs.readFileSync(file));
const provider = config.provider['claude-proxy'];
if (provider.npm !== '@ai-sdk/anthropic') process.exit(9);
if (provider.options.baseURL !== 'https://proxy.example/v1') process.exit(8);
"""
    result = run_opencode_bootstrap(env, child, registry, config_dir=real)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "url, accepted",
    [
        ("https://api.example.com/v1", True),
        ("http://localhost:11434/v1", True),
        ("http://ollama.local:11434/v1", True),
        ("http://192.168.178.85:11434/v1", True),
        ("http://10.0.0.8:11434/v1", True),
        ("http://100.64.1.2:11434/v1", True),
        ("http://host.docker.internal:11434/v1", False),
        ("http://ollama:11434/v1", False),
        ("http://203.0.113.5:11434/v1", False),
    ],
)
def test_jcode_plain_http_hosts_follow_its_own_rule(registry, url, accepted):
    from vibepod.core.provider_adapters import jcode_accepts_url

    assert jcode_accepts_url(url) is accepted
    save_provider(Provider("srv", "openai-chat", url, models=("m",), default_model="m"))
    if accepted:
        prepare_provider("jcode", ["srv"], {})
    else:
        with pytest.raises(ValueError, match="LAN IP"):
            prepare_provider("jcode", ["srv"], {})


@pytest.mark.parametrize(
    "agent, protocol, routing",
    [
        ("pi", "openai-chat", ["--provider", "tuned"]),
        ("codex", "openai-responses", None),
        ("tau", "openai-chat", ["--provider", "tuned"]),
        ("jcode", "openai-chat", ["--provider-profile", "tuned"]),
    ],
)
def test_explicit_model_drops_default_model_settings_arguments(registry, agent, protocol, routing):
    from vibepod.core.provider_launch import prepare_launch

    _save_with_settings(registry, protocol=protocol)
    launch = prepare_launch(agent, ["tuned"], {})
    with_default = launch.arguments([])
    overridden = launch.arguments(["--model", "other"])
    assert "--model" in with_default and "big" in with_default
    joined = " ".join(overridden)
    assert "big" not in overridden and "--model" not in overridden
    assert "--thinking" not in overridden
    assert "model_context_window" not in joined and "model_reasoning_effort" not in joined
    if routing is not None:
        assert overridden == routing
    else:
        assert overridden and all(arg == "-c" or "=" in arg for arg in overridden)
