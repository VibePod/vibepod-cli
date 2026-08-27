# Editor integration (ACP mode)

VibePod can act as an [Agent Client Protocol](https://agentclientprotocol.com/)
(ACP) adapter. ACP is an editor-agnostic, JSON-RPC-based protocol: any editor
or client with ACP support can launch `vp run <agent> --acp` as a subprocess
and embed the containerized agent directly in its AI panel — instead of only
the integrated terminal. Everything that makes a VibePod run what it is —
container isolation, profiles, project overlays, the MITM proxy and local
metric collection — stays active.

## Supported agents

`claude`, `gemini`, `qwen` and `codex` ship an ACP adapter command. Other
agents abort with an error listing the supported agents (you can still provide
your own adapter via `agents.<agent>.acp_command` in the config).

## Setup

Register `vp` as an external/custom agent server in your editor. The exact
location and format depend on the editor; the general shape is a command plus
arguments. For example, in [Zed](https://zed.dev/docs/ai/external-agents)
(`settings.json`):

```json
{
  "agent_servers": {
    "VibePod Claude": {
      "type": "custom",
      "command": "vp",
      "args": ["run", "claude", "--acp"],
      "env": {}
    }
  }
}
```

Repeat the block for `gemini`, `qwen` or `codex` (adjust the `run` argument)
if you want more than one. Make sure `vp` is on the `PATH` your editor
inherits (or use an absolute path).

Then allow your project directory once — under an editor, stdin is a pipe, so
the interactive allow prompt cannot run:

```bash
vp config allow-dir /path/to/your/project
```

Open the AI/agent panel in your editor, pick the VibePod thread type, and
start a thread inside your project.

## How it works

`vp run <agent> --acp` starts the container **without a TTY**, attaches before
the entrypoint runs (so the first JSON-RPC frames are not lost), and
demultiplexes the Docker stream: container stdout carries only the
newline-delimited JSON-RPC stream, all VibePod messages go to stderr. The
workspace is mounted a second time onto its own host path so absolute paths
from the editor (session cwd, @-mentions, diffs) resolve identically inside
the container.

When you close the thread, the editor kills the `vp` process; the container
sees stdin EOF, the adapter exits, and `auto_remove` cleans up — no orphaned
containers.

## Limitations

- **Windows hosts are not supported**: the path-parity mount requires the host
  workspace path to be a valid container path.
- The workspace host path must not collide with container-reserved paths
  (`/workspace`, `/config`, `/claude`, `/qwen`, `/etc`, `/usr`,
  `/tmp/.X11-unix` and the agent's config mount); `--acp` aborts with a clear
  message if it does.
- `--acp` cannot be combined with `--detach`; the ACP client owns the process
  lifetime.
- `--ikwid` is ignored — permissions are negotiated by the editor over ACP.

## Debugging

VibePod's diagnostics (e.g. a missing `vp config allow-dir`) go to stderr and
show up in the editor's ACP logs. Zed, for example, exposes them via
`dev: open acp logs` from the command palette.
