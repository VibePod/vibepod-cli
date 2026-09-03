# Codex Lifecycle Hooks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace VibePod's legacy Codex `notify` wiring with coexistable lifecycle hooks for accurate dash and herdr state reporting, without changing agent images.

**Architecture:** Add one shared JSON-preserving hook registrar in `vibepod.core.codex_hooks`, then have dash and herdr register independent commands for the same Codex lifecycle events. Each POSIX adapter consumes hook JSON on stdin and translates it to its existing bounded reporter; doctor commands inspect the shared hook configuration.

**Tech Stack:** Python 3.10+, JSON/TOML configuration, POSIX shell, pytest, Ruff, mypy.

---

## File structure

- Create `src/vibepod/core/codex_hooks.py`: shared lifecycle event list, idempotent `hooks.json` merge, legacy VibePod `notify` cleanup, and diagnostic lookup.
- Create `tests/test_codex_hooks.py`: focused tests for merge, coexistence, preservation, migration, and malformed input.
- Modify `src/vibepod/core/herdr.py`: delegate Codex registration to the shared helper.
- Modify `src/vibepod/resources/herdr/codex/herdr-agent-state.sh`: consume lifecycle JSON from stdin and map all supported events.
- Modify `tests/test_herdr.py`: replace legacy notify assertions and exercise Codex state/session reporting.
- Create `src/vibepod/resources/codex/dash-agent-state.sh`: VibePod-owned lifecycle adapter that calls the unchanged vendored dash reporter.
- Modify `src/vibepod/core/dash.py`: copy the new adapter and delegate registration to the shared helper.
- Modify `tests/test_dash.py`: assert lifecycle registration, coexistence, and HTTP state mapping.
- Modify `src/vibepod/commands/doctor.py`: detect and probe Codex lifecycle hooks through stdin.
- Modify `docs/dash.md` and `docs/herdr.md`: document mappings and Codex hook trust.

### Task 1: Shared Codex lifecycle-hook registrar

**Files:**
- Create: `src/vibepod/core/codex_hooks.py`
- Create: `tests/test_codex_hooks.py`

- [ ] **Step 1: Write failing creation and merge tests**

Create tests that express the public helper API and exact JSON shape:

```python
from __future__ import annotations

import json
from pathlib import Path

from vibepod.core import codex_hooks


def commands(path: Path, event: str) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        hook["command"]
        for group in data["hooks"][event]
        for hook in group.get("hooks", [])
        if hook.get("type") == "command"
    ]


def test_register_creates_every_lifecycle_event(tmp_path: Path) -> None:
    codex_hooks.register(tmp_path, "/config/.codex/dash-agent-state.sh", label="dash")
    path = tmp_path / ".codex" / "hooks.json"
    for event in codex_hooks.LIFECYCLE_EVENTS:
        assert commands(path, event) == ["/config/.codex/dash-agent-state.sh"]


def test_register_preserves_user_hooks_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "description": "mine",
                "hooks": {
                    "Stop": [{"hooks": [{"type": "command", "command": "mine.sh"}]}],
                    "PreCompact": [{"hooks": [{"type": "command", "command": "compact.sh"}]}],
                },
            }
        ),
        encoding="utf-8",
    )
    command = "/config/.codex/herdr-agent-state.sh"
    codex_hooks.register(tmp_path, command, label="herdr")
    first = path.read_text(encoding="utf-8")
    codex_hooks.register(tmp_path, command, label="herdr")
    assert path.read_text(encoding="utf-8") == first
    data = json.loads(first)
    assert data["description"] == "mine"
    assert commands(path, "Stop") == ["mine.sh", command]
    assert commands(path, "PreCompact") == ["compact.sh"]


def test_dash_and_herdr_handlers_coexist(tmp_path: Path) -> None:
    dash = "/config/.codex/dash-agent-state.sh"
    herdr = "/config/.codex/herdr-agent-state.sh"
    codex_hooks.register(tmp_path, herdr, label="herdr")
    codex_hooks.register(tmp_path, dash, label="dash")
    path = tmp_path / ".codex" / "hooks.json"
    for event in codex_hooks.LIFECYCLE_EVENTS:
        assert commands(path, event) == [herdr, dash]
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
VP_CONFIG_DIR="$(mktemp -d /tmp/vp-config.XXXXXX)" python -m pytest tests/test_codex_hooks.py -v
```

Expected: collection fails because `vibepod.core.codex_hooks` does not exist.

- [ ] **Step 3: Implement the minimal JSON merge**

Create the module with this interface and behavior:

```python
"""Shared Codex lifecycle-hook registration for VibePod integrations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vibepod.utils.console import warning

LIFECYCLE_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PermissionRequest",
    "Stop",
    "Interrupt",
    "SessionEnd",
)

LEGACY_NOTIFY_LINES = frozenset(
    {
        'notify = ["/config/.codex/herdr-agent-state.sh"]',
        'notify = ["/config/.codex/dash-agent-state.sh"]',
    }
)


def _entry(command: str) -> dict[str, Any]:
    return {"hooks": [{"type": "command", "command": command}]}


def register(config_dir: Path, command: str, *, label: str) -> bool:
    """Merge one VibePod command into all Codex lifecycle events."""
    path = config_dir / ".codex" / "hooks.json"
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            warning(f"{label}: could not parse codex hooks.json, skipping hook registration")
            return False
        if not isinstance(loaded, dict):
            warning(f"{label}: codex hooks.json is not an object, skipping hook registration")
            return False
        data = loaded
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        warning(f"{label}: codex hooks.json 'hooks' is not an object, skipping")
        return False
    changed = False
    for event in LIFECYCLE_EVENTS:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            warning(f"{label}: codex hooks.json hooks['{event}'] is not a list, skipping")
            continue
        present = any(
            hook.get("type") == "command" and hook.get("command") == command
            for group in groups
            if isinstance(group, dict)
            for hook in group.get("hooks", [])
            if isinstance(hook, dict)
        )
        if not present:
            groups.append(_entry(command))
            changed = True
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    _remove_legacy_notify(config_dir, label=label)
    return True
```

Implement `registered(config_dir, command)` by safely loading `hooks.json` and
checking every configured group for the exact command. It returns `False` on
missing or malformed files and never warns, because doctor owns presentation.

- [ ] **Step 4: Add failing migration and malformed-input tests**

Append tests that require exact cleanup and soft failure:

```python
import pytest


@pytest.mark.parametrize("legacy", sorted(codex_hooks.LEGACY_NOTIFY_LINES))
def test_register_removes_legacy_vibepod_notify(tmp_path: Path, legacy: str) -> None:
    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(f'model = "gpt"\n{legacy}\n', encoding="utf-8")
    codex_hooks.register(tmp_path, "/config/.codex/dash-agent-state.sh", label="dash")
    assert legacy not in path.read_text(encoding="utf-8")
    assert 'model = "gpt"' in path.read_text(encoding="utf-8")


def test_register_preserves_user_notify(tmp_path: Path) -> None:
    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text('notify = ["my-notifier"]\n', encoding="utf-8")
    codex_hooks.register(tmp_path, "/config/.codex/dash-agent-state.sh", label="dash")
    assert path.read_text(encoding="utf-8") == 'notify = ["my-notifier"]\n'


def test_register_leaves_malformed_hooks_unchanged(tmp_path: Path, capsys) -> None:
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")
    assert not codex_hooks.register(tmp_path, "hook.sh", label="dash")
    assert path.read_text(encoding="utf-8") == "{broken"
    assert "hooks.json" in capsys.readouterr().out
```

- [ ] **Step 5: Verify the migration tests fail for the expected missing helper**

Run the focused file again. Expected: merge tests pass; migration tests fail
because `_remove_legacy_notify` is not defined or does not remove managed lines.

- [ ] **Step 6: Implement exact legacy cleanup**

Add `_remove_legacy_notify`. Read `config.toml`, validate it with `tomllib`,
then remove only lines whose stripped form belongs to `LEGACY_NOTIFY_LINES`.
Preserve a trailing newline. On malformed TOML, warn and leave the file
unchanged. Do not create `config.toml` when it does not exist.

- [ ] **Step 7: Run focused tests and commit**

Run `tests/test_codex_hooks.py`; expect all tests to pass. Then commit:

```bash
git add src/vibepod/core/codex_hooks.py tests/test_codex_hooks.py
git commit -m "feat: register shared Codex lifecycle hooks"
```

### Task 2: Herdr lifecycle adapter

**Files:**
- Modify: `src/vibepod/core/herdr.py`
- Modify: `src/vibepod/resources/herdr/codex/herdr-agent-state.sh`
- Modify: `tests/test_herdr.py`

- [ ] **Step 1: Replace legacy registration tests with lifecycle assertions**

Update tests to call `herdr.register_codex_hooks(tmp_path)`, assert every event
contains `/config/.codex/herdr-agent-state.sh`, assert repeated registration is
byte-idempotent, and assert `apply_herdr_if_enabled("codex", ...)` creates
`.codex/hooks.json`.

- [ ] **Step 2: Add a failing executable adapter test**

Use the existing unix-socket test server and invoke the injected Codex script
with lifecycle JSON on stdin:

```python
@pytest.mark.parametrize(
    ("event", "state"),
    [
        ("UserPromptSubmit", "working"),
        ("PreToolUse", "working"),
        ("PostToolUse", "working"),
        ("PermissionRequest", "blocked"),
        ("Stop", "idle"),
        ("Interrupt", "idle"),
        ("SessionEnd", "idle"),
    ],
)
def test_codex_lifecycle_hook_reports_state(
    event: str,
    state: str,
    monkeypatch,
    sock_dir: Path,
    tmp_path: Path,
) -> None:
    received: list[dict] = []
    thread = _serve_one(sock_dir / "herdr.sock", received)
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    herdr.sync_herdr_files("codex", config_dir, {})
    subprocess.run(
        ["sh", str(config_dir / ".codex" / "herdr-agent-state.sh")],
        input=json.dumps({"hook_event_name": event, "session_id": "s1"}),
        capture_output=True,
        text=True,
        check=True,
        env={
            "PATH": os.environ["PATH"],
            "HERDR_SOCKET_PATH": str(sock_dir / "herdr.sock"),
            "HERDR_PANE_ID": "w1:p1",
            "HOME": str(config_dir),
        },
    )
    thread.join(timeout=5)
    assert received[0]["params"]["state"] == state
```

Add a separate `SessionStart` test with a two-request server and assert the
first method is `pane.report_agent_session` with `agent_session_id` and
`agent_session_path`, followed by an idle `pane.report_agent` request.

- [ ] **Step 3: Run the selected Herdr tests and verify RED**

Expected failures: old `notify` API still exists and the adapter reads argv
instead of stdin, so lifecycle cases do not reach the socket.

- [ ] **Step 4: Delegate registration and implement the adapter mapping**

In `herdr.py`, remove its TOML-specific Codex implementation and expose:

```python
from vibepod.core.codex_hooks import register as register_codex_lifecycle_hooks

CODEX_HOOK_COMMAND = "/config/.codex/herdr-agent-state.sh"


def register_codex_hooks(config_dir: Path) -> None:
    register_codex_lifecycle_hooks(config_dir, CODEX_HOOK_COMMAND, label="herdr")
```

Call this wrapper from `apply_herdr_if_enabled`.

Rewrite the Codex adapter to read `payload=$(cat ...)`, extract
`hook_event_name`, `session_id`, and `transcript_path`, reuse the existing
socket/binary `send` shape from the Claude adapter, and implement:

```sh
case "$event" in
    SessionStart)
        send pane.report_agent_session "" "$session_id" "$transcript"
        send pane.report_agent idle "$session_id" ""
        ;;
    UserPromptSubmit|PreToolUse|PostToolUse)
        send pane.report_agent working "$session_id" ""
        ;;
    PermissionRequest)
        send pane.report_agent blocked "$session_id" ""
        ;;
    Stop|Interrupt|SessionEnd)
        send pane.report_agent idle "$session_id" ""
        ;;
    *) log "ignore event=${event:-?}" ;;
esac
```

Capture reporter output for logging and emit nothing on stdout.

- [ ] **Step 5: Run Herdr tests and commit**

Run `tests/test_codex_hooks.py tests/test_herdr.py`; expect all to pass. Commit:

```bash
git add src/vibepod/core/herdr.py src/vibepod/resources/herdr/codex/herdr-agent-state.sh tests/test_herdr.py
git commit -m "fix: report Herdr state from Codex lifecycle hooks"
```

### Task 3: Dash lifecycle adapter

**Files:**
- Create: `src/vibepod/resources/codex/dash-agent-state.sh`
- Modify: `src/vibepod/core/dash.py`
- Modify: `tests/test_dash.py`

- [ ] **Step 1: Write failing registration and resource tests**

Replace notify tests with assertions that `dash.register_codex_hooks` adds the
dash command alongside a pre-existing herdr command. Update resource tests so
Codex injects the unchanged vendored `vpdash-report.sh` and the new
VibePod-owned adapter next to it.

- [ ] **Step 2: Add failing HTTP mapping tests**

Generalize the existing executable-hook test and parameterize Codex events:

```python
@pytest.mark.parametrize(
    ("event", "state"),
    [
        ("SessionStart", "idle"),
        ("UserPromptSubmit", "working"),
        ("PreToolUse", "working"),
        ("PostToolUse", "working"),
        ("PermissionRequest", "blocked"),
        ("Stop", "idle"),
        ("Interrupt", "idle"),
        ("SessionEnd", "done"),
    ],
)
def test_codex_lifecycle_hook_reports_over_http(
    event: str,
    state: str,
    dash_server: Any,
    tmp_path: Path,
) -> None:
    target = dash.make_target(
        "codex",
        Path("/work/proj"),
        {
            "dash": {"url": server_url(dash_server), "token": "t0ken"},
        },
    )
    assert target is not None
    dash.sync_dash_files("codex", tmp_path, {})
    subprocess.run(
        [str(tmp_path / ".codex" / "dash-agent-state.sh")],
        input=json.dumps(
            {
                "hook_event_name": event,
                "session_id": "s1",
                "cwd": "/work/proj",
                "prompt": "fix it",
                "tool_name": "Bash",
            }
        ),
        text=True,
        check=True,
        env={
            **os.environ,
            **dash.container_env(target, str(tmp_path)),
            "VPDASH_URL": server_url(dash_server),
        },
    )
    assert dash_server.received[0][0]["state"] == state
    assert dash_server.received[0][0]["event"] == event
```

- [ ] **Step 3: Run selected Dash tests and verify RED**

Expected failures: lifecycle registration API/resource does not exist and the
legacy vendored adapter does not parse hook stdin.

- [ ] **Step 4: Wire the local adapter without modifying vendored clients**

Change dash's built-in resource root to the package `resources` directory and
prefix existing vendored paths with `dash/`. For Codex use:

```python
"codex": [
    ("dash/vpdash-report.sh", ".codex/vpdash-report.sh"),
    ("codex/dash-agent-state.sh", ".codex/dash-agent-state.sh"),
],
```

Expose `CODEX_HOOK_COMMAND` and `register_codex_hooks` through the shared
registrar, then call it from `apply_dash_if_enabled`.

Create the POSIX adapter. It reads stdin, extracts common fields, verifies the
vendored reporter, and maps:

```sh
case "$event" in
    SessionStart) state=idle; message="session started" ;;
    UserPromptSubmit) state=working; message=$(field prompt) ;;
    PreToolUse|PostToolUse) state=working; message=$(field tool_name) ;;
    PermissionRequest)
        state=blocked
        message=$(field description)
        [ -n "$message" ] || message="$(field tool_name) needs approval"
        ;;
    Stop) state=idle; message=$(field last_assistant_message) ;;
    Interrupt) state=idle; message="turn interrupted" ;;
    SessionEnd) state=done; message="session ended" ;;
    *) log "ignore event=${event:-?}"; exit 0 ;;
esac
```

Call `vpdash-report.sh` with the state, exact event, message, session id, and
cwd. Capture all reporter output into the trace log so the hook produces no
stdout control payload.

- [ ] **Step 5: Run Dash tests and commit**

Run `tests/test_codex_hooks.py tests/test_dash.py`; expect all to pass. Commit:

```bash
git add src/vibepod/core/dash.py src/vibepod/resources/codex/dash-agent-state.sh tests/test_dash.py
git commit -m "fix: report Dash state from Codex lifecycle hooks"
```

### Task 4: Doctor and documentation

**Files:**
- Modify: `src/vibepod/commands/doctor.py`
- Modify: `tests/test_dash.py`
- Modify: `tests/test_herdr.py`
- Modify: `docs/dash.md`
- Modify: `docs/herdr.md`

- [ ] **Step 1: Write failing doctor tests**

Add focused tests that create `.codex/hooks.json` through each integration,
invoke the relevant registration helper used by doctor, and assert the summary
or deep-dive reports `hooks.json`, not `notify`. Add a missing-registration case
that produces `MISSING` without raising on malformed JSON.

- [ ] **Step 2: Run doctor tests and verify RED**

Run the selected doctor tests. Expected: output still says `notify`, and both
in-container probes pass Codex payloads as argv.

- [ ] **Step 3: Update diagnostics and probes**

Import `vibepod.core.codex_hooks` in `doctor.py`. For Codex registration use:

```python
ok = codex_hooks.registered(cfg_dir, dash_core.CODEX_HOOK_COMMAND)
registration = "hooks.json" if ok else "MISSING"
```

Use the corresponding herdr command for herdr diagnostics. Change both Codex
probe payloads to lifecycle JSON such as
`{"hook_event_name":"Stop","session_id":"doctor"}` and pipe stdin for Codex
the same way as the other hook-based agents.

- [ ] **Step 4: Update user documentation**

In both integration docs, state that Codex uses lifecycle hooks, document the
state mapping, and explain that Codex may require one-time review through
`/hooks`. Remove the dash limitation about the single notify program and the
herdr statement that Codex lacks working/blocked events.

- [ ] **Step 5: Run focused tests and commit**

Run:

```bash
VP_CONFIG_DIR="$(mktemp -d /tmp/vp-config.XXXXXX)" python -m pytest \
  tests/test_codex_hooks.py tests/test_dash.py tests/test_herdr.py -v
```

Commit:

```bash
git add src/vibepod/commands/doctor.py tests/test_dash.py tests/test_herdr.py docs/dash.md docs/herdr.md
git commit -m "docs: describe Codex lifecycle state reporting"
```

### Task 5: Full verification

**Files:**
- Modify if necessary: files already listed above

- [ ] **Step 1: Run the full hermetic unit suite**

```bash
VP_CONFIG_DIR="$(mktemp -d /tmp/vp-config.XXXXXX)" python -m pytest
```

Expected: all non-integration tests pass.

- [ ] **Step 2: Run static checks**

```bash
python -m ruff check .
python -m ruff format --check .
python -m mypy src
git diff --check
```

Expected: every command exits zero with no new warnings.

- [ ] **Step 3: Inspect the final diff against the design**

Confirm no image constants, Dockerfiles, or vendored files under
`src/vibepod/resources/dash/` changed. Confirm only exact VibePod legacy notify
lines are removed, both commands coexist in `hooks.json`, and every report
path remains soft-failing.

- [ ] **Step 4: Request code review and address findings**

Review the complete diff from the design commit through `HEAD`, fix every
critical or important finding test-first, and rerun the checks above.
