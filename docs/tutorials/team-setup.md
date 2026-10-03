# Share a team setup

In this tutorial you commit a VibePod setup to a project repository so that
everyone who clones it gets the same agent defaults and the same tools inside
the agent container. Credentials stay on each person's machine. You will use:

- a [project config](../configuration.md#project-level-config)
  (`.vibepod/config.yaml`) for shared defaults
- a [project overlay](../overlays/index.md) (`.vibepod/overlay/`) to install
  tools into the agent image
- [credential profiles](../profiles.md) and your personal global config for
  everything that differs per person

The example project is called `my-app` and the team uses Claude by default.

## 1. Create the project config

From the repository root, run:

```bash
cd ~/my-app
vp config init
```

This creates `.vibepod/config.yaml` with a minimal starter (`version: 1`).

!!! note "Run `vp` from the repository root"
    VibePod reads `.vibepod/config.yaml` from the **current directory**, not
    from the directory passed with `-w`. Start agents from the repository root
    so the shared config applies.

## 2. Add the shared defaults

Open `.vibepod/config.yaml` and add only the settings the whole team should
share. For example, make Claude the default agent and publish the port of the
project's dev server:

```yaml
# .vibepod/config.yaml
version: 1
default_agent: claude
agents:
  claude:
    ports:
      - "127.0.0.1:3000:3000" # dev server started by the agent
    env:
      APP_ENV: development # non-secret project settings only
```

With this file in place, `vp run` (no agent name) starts Claude, and the dev
server the agent starts inside the container is reachable on
`http://127.0.0.1:3000`. See [Publishing ports](../configuration.md#publishing-ports)
and [Mounting volumes](../configuration.md#mounting-volumes) for the other
per-agent keys.

Check the merged result:

```bash
vp config show
```

!!! warning "Review `vp config init <agent>` output before committing"
    `vp config init claude` copies your **effective** `agents.claude` block
    into the project file: the built-in defaults merged with your global
    config. If your global config holds personal values for that agent (an
    API key in `env`, a private image, local volumes), they end up in the
    project file. Remove everything the team should not share before you
    commit, or write the block by hand as above.

## 3. Add an overlay to install tools

The agent images do not include every tool a project needs. A project overlay
is a `FROM`-less Dockerfile fragment that VibePod appends to the agent image
and builds once per project. Create `.vibepod/overlay/Dockerfile`:

```dockerfile
# .vibepod/overlay/Dockerfile — no FROM line
RUN apt-get update && apt-get install -y --no-install-recommends \
        jq ripgrep \
    && rm -rf /var/lib/apt/lists/*
```

A fragment in `.vibepod/overlay/Dockerfile` applies to every agent in the
project. To target one agent, put it in `.vibepod/overlay/<agent>/Dockerfile`
instead (for example `.vibepod/overlay/claude/Dockerfile`); it wins over the
shared one for that agent.

Start the agent. The first run builds the overlay image; later runs reuse it
until the fragment, the files next to it, or the base image change:

```bash
vp run claude
```

To see which overlay image each agent resolves to, and whether it is built,
run `vp list` and look at the **Project Overlays** table.

More fragments (Python requirements, pixi, a PDF/OCR toolchain) are on the
[overlay recipes](../overlays/recipes.md) page. For a one-off run without the
overlay, use `vp run claude --no-overlay`.

## 4. Keep credentials and personal settings out of the repository

VibePod does not expand variables inside config files, so any value you write
into `.vibepod/config.yaml` is committed as is. Keep secrets in one of these
places instead:

- **Agent logins.** Each person logs in once with `vp run <agent>`. The
  credentials are stored under `~/.config/vibepod/agents/<agent>/` (or the
  active profile's directory), outside the repository.
- **Your global config.** Put personal environment variables, such as API
  keys, into `~/.config/vibepod/config.yaml`:

    ```yaml
    # ~/.config/vibepod/config.yaml — personal, never committed
    agents:
      claude:
        env:
          ANTHROPIC_API_KEY: sk-ant-...
    ```

    `env` maps are merged key by key, so this personal key and the project's
    `APP_ENV` both reach the container. Lists such as `ports`, `volumes` and
    `init` are not merged: a project list replaces the global one.

- **The command line.** Pass a value for a single run with `-e`:

    ```bash
    vp run claude -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY"
    ```

## 5. Use profiles for per-person variants

[Credential profiles](../profiles.md) let one person keep several logins per
agent, for example a subscription login and a work API key. Profiles live in
each person's home directory (`~/.config/vibepod/profiles/<name>/`), so they
are not part of the repository:

```bash
vp profile create work
vp run claude --profile work    # log in once; the login is stored in "work"
```

To use a profile without the flag, set `VP_PROFILE=work` in your shell or
`profile: work` in your global config.

!!! warning "Pinning a profile in the project config"
    A `profile:` key in `.vibepod/config.yaml` applies to everyone who runs
    VibePod in the project, and referencing a profile that does not exist is
    an error. Only commit it if the team agrees on the profile name, and tell
    everyone to run `vp profile create <name>` first.

## 6. Commit the setup

| Commit | Keep local |
| ------ | ---------- |
| `.vibepod/config.yaml` (shared, non-secret settings) | `~/.config/vibepod/config.yaml` (your global config, API keys) |
| `.vibepod/overlay/` (Dockerfile fragments and the files they `COPY`) | `~/.config/vibepod/agents/` and `~/.config/vibepod/profiles/` (logins) |
| | `~/.vibepod/providers/` ([model providers](../providers.md), stored keys) |
| | `~/.config/vibepod/logs.db`, `proxy/`, `tasks.db` (local logs) |

```bash
git add .vibepod/config.yaml .vibepod/overlay/
git commit -m "Add a shared VibePod setup"
```

Project skills installed with `vp skills add ... --scope local` also live under
`.vibepod/` (in `.vibepod/skills/`); see [Skills > Scopes](../skills/index.md#scopes).

!!! warning "Review `.vibepod/` changes like code"
    A committed project config can mount any host path the user can read and
    an overlay runs arbitrary build steps. Review changes to `.vibepod/` in
    pull requests, and check the `volumes` of a project you did not write
    before running an agent in it.

## 7. What a teammate does

After cloning the repository, a teammate runs:

```bash
cd my-app
vp run            # starts the default agent from the project config
```

VibePod asks to add the directory to the allow list, builds the overlay image
on the first run, and starts Claude. The teammate logs in once; the login is
stored in their own `~/.config/vibepod/`.

## Next steps

- [Configuration](../configuration.md): every key you can set in the project
  config, including proxy allow/deny filters.
- [Project overlays](../overlays/index.md): caching, rebuilds, and the
  `overlay: false` switch.
- [Use your own model provider](own-model-provider.md): route the team's agents
  to a shared model server.
