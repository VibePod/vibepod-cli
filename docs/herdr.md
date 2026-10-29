# Herdr integration

[herdr](https://herdr.dev/) is a terminal multiplexer for coding agents. When
you start `vp run <agent>` inside a herdr pane, VibePod detects it
automatically and wires the container so the agent appears in herdr with live
state (working / blocked / idle) and session identity:

- the herdr unix socket (and, when usable, the host `herdr` binary) is
  mounted into the container — or, where the container engine cannot mount
  sockets, an events file is relayed instead (see
  [Socket or file relay](#socket-or-file-relay))
- `HERDR_*` environment variables are forwarded
- when the agent exposes lifecycle hooks, a VibePod-managed integration is
  placed in its config directory and reports events to the socket API (or
  the events file)

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

## Socket or file relay

On Linux the herdr socket is bind-mounted into the container and the agent
reports to herdr directly.

Off Linux every container engine (Docker Desktop, Colima, Podman machine)
runs inside a VM whose file share cannot carry socket inodes, so the socket
cannot be mounted. Regular files do cross that share, so state travels
through a file instead:

1. `vp run` creates a per-run directory under the VibePod config directory
   (`~/.config/vibepod/herdr-relay/`), mounts it at `/herdr-events` and sets
   `HERDR_EVENTS_FILE=/herdr-events/herdr-events.jsonl`. `HERDR_SOCKET_PATH`
   stays unset.
2. Reporters inside the container append one JSON line per event — the
   `params` of a `pane.report_agent` request — to that file.
3. The attached `vp run` on the host tails the file (every 250 ms) and
   forwards each new line to the herdr socket as `pane.report_agent`, in
   order. The directory is removed when the run ends.

The container is not trusted to send arbitrary socket requests. A line is
forwarded only when it is a JSON object of at most 4 KB with the string
fields `pane_id`, `source`, `agent` and `state` and, optionally,
`display_agent` and `agent_session_id`. `pane_id` must be the run's own
`HERDR_PANE_ID` and `state` one of `working`, `blocked` or `idle`. Anything
else is dropped (logged at debug level).

`vp doctor herdr` shows which transport is in use, and
`vp doctor herdr <agent>` replays a hook through it to check that events
reach herdr.

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

When the socket cannot be mounted (see
[Socket or file relay](#socket-or-file-relay)), neither `HERDR_SOCKET_PATH`
nor `HERDR_BIN_PATH` is set, but `HERDR_EVENTS_FILE` is. Append one
`pane.report_agent` params object per line, in a single write of under 4 KB
so concurrent hooks don't interleave. The relay only forwards lines for the
run's own pane with `source` `vibepod`, `agent` set to the run's agent id and,
if given, `display_agent` `vp:<agent>`:

```sh
if [ -n "${HERDR_SOCKET_PATH:-}" ]; then
    : # report over the socket or the herdr binary, as above
elif [ -n "${HERDR_EVENTS_FILE:-}" ]; then
    printf '{"pane_id":"%s","source":"vibepod","agent":"gemini","state":"working"}\n' \
        "$HERDR_PANE_ID" >> "$HERDR_EVENTS_FILE"
fi
```

## Limitations

- Windows hosts are skipped entirely: herdr uses named pipes there (not a
  filesystem Unix socket), so neither the container wiring nor the host-side
  pane report works; `vp run` simply runs without herdr integration.
- Off Linux, live state needs the attached `vp run` in the herdr pane: it is
  the process that relays the events file. Detached runs (`vp run --detach`,
  `vp task`) get no events file and only report the `vp:<agent>` pane
  identity from the host, so the agent appears in herdr without
  working/blocked/idle transitions.
- The file relay carries `pane.report_agent` state only. Claude's
  `pane.report_agent_session` (the transcript path, meaningless on the host)
  is not relayed.
- On Linux the socket is still mounted: it keeps reports immediate, where the
  relay adds up to a quarter second of latency.
- Shell/JavaScript hooks need `node` in the agent image or a `herdr` binary
  that can run inside the container. Tau instead uses its installed Python
  runtime. Homebrew-on-Linux and musl-linked host binaries may not run in stock
  images; direct socket integrations avoid that dependency. For custom agents
  without a compatible runtime, add one via an [overlay](overlays/index.md).
- `vp doctor herdr [agent]` diagnoses the whole chain from inside a pane.
