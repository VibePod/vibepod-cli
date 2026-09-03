# Dash integration

[VibePod Dash](https://github.com/VibePod/vibepod-dash) is a small web
dashboard for the state of your running agents — the same idea as the
[herdr](herdr.md) sidebar, but in a browser, so you can check on your agents
from your phone.

Point VibePod at a dash server and every `vp run` and `vp task create` shows up
on the board with a live state (working / blocked / idle / done / error):

```bash
export VPDASH_URL=http://localhost:8765
export VPDASH_TOKEN=<ingest token printed by the dash server>
vp run claude
```

Or, permanently, in `~/.config/vibepod/config.yaml` (or the project's
`.vibepod/config.yaml`):

```yaml
dash:
  url: http://localhost:8765
  token: s3cret
```

## What VibePod wires up

- `VPDASH_*` environment is injected into the container, including a stable
  agent id and the display name `vp:<agent> · <project>`
- for agents with lifecycle hooks, a VibePod-managed reporter plus hook script
  is copied into the agent's config dir and registered
- the CLI itself reports the container's start and stop, so **every** agent
  appears on the board even without hooks
- `vp stop`, `vp task cancel` and a finished task mark the card done

Built-in hook reporting ships for **claude**, **codex** and **copilot**. The
hooks need only `curl` inside the image.

| Claude hook event  | reported state          |
| ------------------ | ----------------------- |
| `SessionStart`     | `idle`                  |
| `UserPromptSubmit` | `working` (your prompt) |
| `PreToolUse`       | `working` (tool name)   |
| `PostToolUse`      | `working` (tool name)   |
| `Notification`     | `blocked` (the message) |
| `Stop`             | `idle`                  |
| `SessionEnd`       | `done`                  |

`blocked` is the state worth a phone notification: the agent is waiting for
your approval.

## Reaching the dashboard from inside the container

`localhost` inside a container is the container itself, so a `localhost` or
`127.0.0.1` dash URL is rewritten to `host.docker.internal` for the agent —
VibePod maps that name to the host gateway on every run. The CLI keeps using
the original URL for its own reports. A dash server on another machine (or in
another container) is passed through unchanged.

## Identity on the board

One card per agent *and* workspace: the id is derived from host, agent and
workspace path, so re-running an agent in the same checkout updates the card it
had before instead of stacking up a new one. Override it per run with
`VPDASH_AGENT_ID` (one card per run) or rename the card with
`VPDASH_AGENT_NAME`.

## Opting out

- `vp run <agent> --no-dash` / `vp task create ... --no-dash` — skip one run
- `dash: false` (or `dash: {enabled: false}`) in the config — disable entirely
- no URL configured — nothing is wired up at all, which is the default

## Custom agents

Like herdr, file injection is data-driven. To wire an agent without built-in
support, map host files into the agent's config directory:

```yaml
dash:
  url: http://localhost:8765
  integrations:
    gemini:
      - source: ~/.config/my-hooks/gemini-dash.sh
        dest: hooks/gemini-dash.sh
```

Inside the container the script finds `VPDASH_URL`, `VPDASH_TOKEN`,
`VPDASH_AGENT`, `VPDASH_AGENT_ID`, `VPDASH_AGENT_NAME`, `VPDASH_HOST` and
`VPDASH_LOG` in the environment. Reporting is one HTTP call:

```sh
curl -sS -X POST "$VPDASH_URL/api/v1/events" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $VPDASH_TOKEN" \
    -d '{"agent_id":"'"$VPDASH_AGENT_ID"'","state":"working","message":"…"}'
```

## Troubleshooting

`vp doctor dash` prints the resolved configuration, checks that the board
answers, and summarises every agent's integration state. `vp doctor dash
<agent>` goes deeper: injected files, registration, the hook trace log
(`<agent config dir>/dash-hook.log`), a live host-side report, and a probe that
runs the real hook inside the agent image — which is what proves the container
can reach the dashboard.

## Limitations

- Codex allows a single `notify` program. When herdr already claimed it, dash
  leaves it alone and falls back to container start/stop reports.
- Reports are HTTP calls from inside the container; an agent image without
  `curl` can only be tracked by the CLI-side start/stop reports.
- The dashboard sees whatever the agent reports — prompts, tool names,
  notification text. Treat it as sensitive as the sessions it watches, and put
  it behind a token (and TLS) when it is reachable beyond your LAN.
