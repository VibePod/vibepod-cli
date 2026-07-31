# Herdr integration

[herdr](https://herdr.dev/) is a terminal multiplexer for coding agents. When
you start `vp run <agent>` inside a herdr pane, VibePod detects it
automatically and wires the container so the agent appears in herdr with live
state (working / blocked / idle) and session identity:

- the herdr unix socket (and, when usable, the host `herdr` binary) is
  mounted into the container
- `HERDR_*` environment variables are forwarded
- when the agent exposes lifecycle hooks, a VibePod-managed integration is
  placed in its config directory and reports events directly to the socket API

Built-in state reporting ships for **claude**, **codex**, **copilot**,
**opencode**, **pi**, and **tau**. Every supported agent receives a canonical
agent identity plus a visible `vp:<agent>` display name and initial idle state.
Agents without lifecycle hooks still appear in herdr, but cannot report reliable
working/blocked/idle transitions.

Tau uses its public Python extension API (`~/.tau/extensions/`) to report
working and idle lifecycle events without requiring Node. Agy receives the
`vp:agy` pane identity and initial state, but its proprietary CLI currently has
no documented hook or extension API for live state transitions.

No setup is needed. Detection uses `HERDR_ENV=1`, which herdr sets only
inside its panes.

## Opting out

- `vp run <agent> --no-herdr` — skip wiring for one run
- `herdr: false` in `.vibepod/config.yaml` or the global config — disable
  entirely

## Custom agents

The file injection is data-driven. To wire an agent without built-in support
(or add extra files for a built-in one), map host files into the agent's
config directory:

```yaml
herdr:
  integrations:
    gemini:
      - source: ~/.config/my-hooks/gemini-herdr.sh
        dest: hooks/gemini-herdr.sh
```

Inside the container the script finds `HERDR_PANE_ID` and `HERDR_SOCKET_PATH`
already set. When VibePod also found a usable `herdr` binary on the host,
`HERDR_BIN_PATH` is set too and the script can report state with:

```sh
"$HERDR_BIN_PATH" pane report-agent "$HERDR_PANE_ID" \
    --source vibepod --agent gemini --state working
```

`HERDR_BIN_PATH` may be unset (no binary on the host, or one that cannot run
inside the container). Custom integrations should then fall back to the socket
API: send a `pane.report_agent` JSON request over the unix socket at
`HERDR_SOCKET_PATH`, as the bundled `herdr-report.js` reporter does.

## Limitations

- Windows hosts are skipped: herdr uses named pipes there, which cannot be
  bind-mounted into Linux containers.
- Shell/JavaScript hooks need `node` in the agent image or a `herdr` binary
  that can run inside the container. Tau instead uses its installed Python
  runtime. Homebrew-on-Linux and musl-linked host binaries may not run in stock
  images; direct socket integrations avoid that dependency. For custom agents
  without a compatible runtime, add one via an [overlay](overlays.md).
- `vp doctor herdr [agent]` diagnoses the whole chain from inside a pane.
