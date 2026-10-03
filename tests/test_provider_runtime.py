"""Per-agent bootstrap selection and mount installation."""

from pathlib import Path

from vibepod.core import provider_runtime


def test_bootstrap_table_covers_five_agents():
    assert provider_runtime.WRAPPED_AGENTS == frozenset({"pi", "codex", "opencode", "tau", "jcode"})


def test_wrap_uses_per_agent_interpreter():
    import json

    from vibepod.core.provider_runtime import (
        BOOTSTRAP_MOUNT,
        BOOTSTRAP_MOUNT_PY,
        COMMAND_ENV,
    )

    argv, env = provider_runtime.wrap_provider_command("pi", ["pi", "-p", "hi there $(x)"])
    assert argv == ["node", BOOTSTRAP_MOUNT]
    assert json.loads(env[COMMAND_ENV]) == ["pi", "-p", "hi there $(x)"]
    argv, _ = provider_runtime.wrap_provider_command("opencode", ["opencode"])
    assert argv == ["node", BOOTSTRAP_MOUNT]
    argv, _ = provider_runtime.wrap_provider_command("tau", ["tau", "-p", "hi"])
    assert argv == ["python3", BOOTSTRAP_MOUNT_PY]
    argv, env = provider_runtime.wrap_provider_command("jcode", ["jcode", "run", "hi there", ""])
    assert argv == ["sh", provider_runtime.BOOTSTRAP_MOUNT_SH]
    # One base64 word per argument, "b"-prefixed so empty arguments survive splitting.
    import base64

    words = env[provider_runtime.COMMAND_B64_ENV].split(" ")
    assert [base64.b64decode(w[1:]).decode() for w in words] == ["jcode", "run", "hi there", ""]
    assert all(w.startswith("b") for w in words)
    assert " " not in "".join(words)
    assert provider_runtime.wrap_provider_command("claude", ["claude"]) == (["claude"], {})


def test_python_bootstrap_source_is_packaged():
    source = provider_runtime.bootstrap_source("provider-bootstrap.py")
    assert "VIBEPOD_PROVIDER_PLAN" in source
    assert "VIBEPOD_PROVIDER_COMMAND" in source


def test_sh_bootstrap_source_is_packaged():
    source = provider_runtime.bootstrap_source("provider-bootstrap.sh")
    assert source.startswith("#!/bin/sh")
    assert "VIBEPOD_PROVIDER_COMMAND_B64" in source and "JCODE_HOME" in source


def test_bootstrap_volume_installs_agent_specific_script(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_runtime, "get_config_root", lambda: tmp_path)
    host_cjs, mount_cjs, mode = provider_runtime.bootstrap_volume("pi")
    assert mount_cjs == provider_runtime.BOOTSTRAP_MOUNT
    assert mode == "ro"
    assert Path(host_cjs) == tmp_path / "runtime" / "provider-bootstrap.cjs"
    assert Path(host_cjs).is_file()
    host_py, mount_py, _ = provider_runtime.bootstrap_volume("tau")
    assert mount_py == provider_runtime.BOOTSTRAP_MOUNT_PY
    assert Path(host_py) == tmp_path / "runtime" / "provider-bootstrap.py"
    assert Path(host_py).is_file()
    # Repeated calls are idempotent: same mount triple, no rewrite churn.
    assert provider_runtime.bootstrap_volume("tau") == (host_py, mount_py, "ro")
    host_sh, mount_sh, _ = provider_runtime.bootstrap_volume("jcode")
    assert mount_sh == provider_runtime.BOOTSTRAP_MOUNT_SH
    assert Path(host_sh) == tmp_path / "runtime" / "provider-bootstrap.sh"
