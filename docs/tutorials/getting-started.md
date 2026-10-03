# Getting started with VibePod

This tutorial takes you from a fresh install through a realistic first day
with VibePod: starting an agent in a project, leaving and rejoining the
session, trying a second agent, looking at what the agents did, and saving
your settings for next time. Each step explains what VibePod does behind the
command, and links to the reference page with the details.

The examples use Claude and a project at `~/my-project`. Every other
[supported agent](../agents/index.md#supported-agents) works the same way.

## 1. Install and check the container runtime

Install VibePod with pip (other methods, such as conda and pixi, are listed in
the [Quickstart](../quickstart.md#install)):

=== "pip"

    ```bash
    pip install vibepod
    ```

=== "Homebrew"

    ```bash
    brew install vibepod/vibepod/vibepod
    ```

The package installs two equivalent commands, `vp` and `vibepod`. This
tutorial uses `vp`.

Check that VibePod can reach Docker or Podman:

```bash
vp version
```

The output shows the VibePod and Python versions, plus the container runtime
it found. Illustrative output:

```text
VibePod CLI: 0.x.y
Python:      3.12.3
Runtime:     Docker 27.3.1
```

If the runtime line says `unavailable`, start Docker or Podman first. Podman
users should read [Using Podman instead of Docker](../quickstart.md#using-podman-instead-of-docker)
for the socket and DNS setup.

To see every command, run `vp --help`. Each command has its own help, for
example `vp run --help`.

## 2. Start your first agent

Go to the project you want to work on and start an agent:

```bash
cd ~/my-project
vp run claude
```

The first time you run an agent in a directory, VibePod asks whether to allow
it:

```text
'/home/you/my-project' is not allowed for `vp run`. Would you like to allow it?
```

Answer yes, and VibePod remembers the directory. VibePod refuses to run in
your home directory or in `/`, so always start from a project directory. You
can manage the list with `vp config allow-dir`, `vp config list-allowed-dirs`,
and `vp config remove-dir`.

Then VibePod:

1. **Pulls the agent image** (`vibepod/claude:latest`) if it is missing or
   outdated. By default it checks for a newer image on every run; see
   [Auto-pulling the latest image](../agents/index.md#auto-pulling-the-latest-image)
   to turn that off.
2. **Creates the `vibepod-network` network and starts the `vibepod-proxy`
   container**, which records the agent's outgoing HTTP(S) requests for the
   dashboard you will open in step 5.
3. **Mounts your project at `/workspace`** inside the container. The agent
   sees and edits your real files. To mount more host paths, see
   [Mounting volumes](../configuration.md#mounting-volumes).
4. **Mounts the agent's config directory** from
   `~/.config/vibepod/agents/claude/`. Logins, settings and the agent's own
   session history live there, so they survive the container.
5. **Starts the agent and attaches your terminal** to it.

### Log in once

On the first run the agent is not logged in yet. Use its normal login flow
(browser OAuth, an API key, or a device code, depending on the agent). The
credentials land in the mounted config directory, so the next `vp run claude`
starts logged in. Some agents have extra options, such as a long-lived token
for Claude; see [Agents > Individual agents](../agents/index.md#individual-agents).

### Shortcuts

Most agents have a short alias. These three commands do the same thing:

```bash
vp run claude
vp claude
vp c
```

The full list is in the [supported agents table](../agents/index.md#supported-agents).

## 3. Leave the session and come back

Work with the agent as you normally would. Your keystrokes, including
Ctrl+C, go to the agent, so quit it with its own exit command or shortcut.
When the agent process exits, its container stops and is removed (the
`auto_remove` default). Agents keep their session history in the config
directory, and for several agents VibePod prints the command to resume the
session afterwards (see [Resume hints](../agents/index.md#resume-hints-after-a-session-ends)).

Closing the terminal window is different: it does **not** stop the agent.
The container keeps running in the background. Find it and reattach:

```bash
vp list --running       # running VibePod containers
vp attach <container>   # rejoin the session
```

If only one VibePod container is running, `vp attach` without a name is
enough.

### Start in the background

To start an agent without attaching your terminal, use `-d`:

```bash
vp run claude -d
vp attach <container>   # attach when you are ready
```

Sessions started with `-d` are not written to the session log, because
VibePod does not see their terminal I/O. See
[Detached mode](../agents/index.md#detached-mode).

### Stop agents

```bash
vp stop claude                  # every container of the claude agent
vp stop <container>             # one container from `vp list`
vp stop --all                   # every VibePod container, including the proxy
```

## 4. Try another agent

Switching agents is the same command with another name. Your project is
mounted the same way, and each agent has its own config directory and its own
login:

```bash
vp run codex     # or: vp x
vp run gemini    # or: vp g
```

To pass arguments to the agent itself, put them after `--` so VibePod does
not read them as its own options:

```bash
vp run claude -- --help
```

### Skipping permission prompts with `--ikwid`

Agents normally ask before they edit files or run commands. `--ikwid`
("I know what I'm doing") adds the agent's own auto-approval flag, for example
`--dangerously-skip-permissions` for Claude:

```bash
vp run claude --ikwid
```

The trade-off: the agent then acts without asking. The container limits what
it sees to what VibePod mounts, but it can still change or delete anything in
`/workspace` and send data over the network. Use it on work that is committed
or otherwise easy to restore. Not every agent has such a flag; see
[IKWID mode](../agents/index.md#ikwid-mode-ikwid) for the list.

## 5. Look at what happened

VibePod keeps two local databases while agents run: a session log with what
you type into interactive sessions, and the proxy's record of every HTTP(S)
request. Open them in the dashboard:

```bash
vp logs start
```

This starts a Datasette container and opens `http://localhost:8001` in your
browser, where you can browse the agents' traffic and, for Claude Code, token
usage. Use `--port` to pick another port and `--no-open` to skip opening the
browser. All data stays on your machine.

When you are done:

```bash
vp logs stop
```

`vp logs status` shows whether the dashboard is running.

## 6. Make your settings stick

VibePod merges its configuration from built-in defaults, a global file, a
project file, and environment variables. Print the files in use:

```bash
vp config path
```

Illustrative output:

```text
Config:  /home/you/.config/vibepod
Global:  /home/you/.config/vibepod/config.yaml
Project: /home/you/my-project/.vibepod/config.yaml
Logs:    /home/you/.config/vibepod/logs.db
Proxy:   /home/you/.config/vibepod/proxy/proxy.db
```

Settings that are only yours belong in the global file. Settings for one
project go in the project file, which you create with:

```bash
vp config init
```

This writes `.vibepod/config.yaml` with `version: 1`. Add the keys you want to
change. For example, start Codex when you run `vp run` without an agent name,
and publish the port of a dev server the agent starts in its container:

```yaml
# .vibepod/config.yaml
version: 1
default_agent: codex
agents:
  codex:
    ports:
      - "127.0.0.1:3000:3000"
```

VibePod reads the project file from the **current directory**, so run `vp`
from the project root. Check the merged result with `vp config show`.

!!! warning "`vp config init <agent>` copies your merged settings"
    `vp config init claude` adds a full `agents.claude` block to the project
    file. It copies the **effective** settings: the built-in defaults merged
    with your global config. If your global config holds personal values for
    that agent, such as an API key in `env`, they end up in the project file.
    Trim the block before you commit the file, and keep secrets in the global
    config instead.

See [Configuration](../configuration.md) for every key and environment
variable.

## 7. Clean up

At the end of the day, stop everything VibePod started:

```bash
vp stop --all    # agents, the proxy, and the dashboard
```

Your logins, settings and logs stay in `~/.config/vibepod/`, so the next
`vp run` picks up where you left off.

## Next steps

- [Agents](../agents/index.md): per-agent login notes, task mode for headless
  runs, and image customization.
- [Credential profiles](../profiles.md): keep several logins per agent, such
  as a personal subscription and a work API key.
- [Project overlays](../overlays/index.md): install extra tools into the agent
  image for one project.
- [Model providers](../providers.md): run agents against a local or hosted
  model server.
- [Skills](../skills/index.md): add reusable prompts and recipes to agents.
