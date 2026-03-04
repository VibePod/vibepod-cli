# Configuration

VibePod merges configuration from four sources in order, with each layer overriding the previous:

1. **Built-in defaults**
2. **Global config** — `~/.config/vibepod/config.yaml`
3. **Project config** — `.vibepod/config.yaml` in the current directory
4. **Environment variables** — override specific keys at runtime

Run `vp config path` to print the exact paths in use, and `vp config show` to print the fully merged result.

## Full reference

```yaml
# Config format version (always 1)
version: 1

# Agent to run when no argument is given to `vp run`
default_agent: claude

# Container runtime: auto | docker | podman (default: auto)
# See docs/podman.md for setup instructions
container_runtime: auto

# Pull the latest image before every run (default: true)
# Can be overridden per agent with agents.<agent>.auto_pull
auto_pull: true

# Remove the container automatically when it stops (default: true)
auto_remove: true

# Primary Docker network created and used by VibePod
network: vibepod-network

# Log level: debug | info | warning | error
log_level: info

# Disable colour output
no_color: false

agents:
  claude:
    enabled: true
    image: vibepod/claude:latest
    auto_pull: null  # Per-agent override: true/false, or null to use global auto_pull
    env: {}       # Extra environment variables passed to the container
    volumes: []   # Reserved for future use
    init: []      # Optional shell commands run before agent startup

  gemini:
    enabled: true
    image: nezhar/gemini-container:latest
    env: {}
    volumes: []
    init: []

  opencode:
    enabled: true
    image: nezhar/opencode-cli:latest
    env: {}
    volumes: []
    init: []

  devstral:
    enabled: true
    image: nezhar/devstral-cli:latest
    env: {}
    volumes: []
    init: []

  auggie:
    enabled: true
    image: nezhar/auggie-cli:latest
    env: {}
    volumes: []
    init: []

  copilot:
    enabled: true
    image: nezhar/copilot-cli:latest
    env: {}
    volumes: []
    init: []

  codex:
    enabled: true
    image: vibepod/codex:latest
    env: {}
    volumes: []
    init: []

logging:
  enabled: true
  image: vibepod/datasette:latest
  db_path: ~/.config/vibepod/logs.db
  ui_port: 8001         # Port for the Datasette UI

proxy:
  enabled: true
  image: vibepod/proxy:latest
  db_path: ~/.config/vibepod/proxy/proxy.db
  ca_dir: ~/.config/vibepod/proxy/mitmproxy
  ca_path: ~/.config/vibepod/proxy/mitmproxy/mitmproxy-ca-cert.pem
```

## Environment variables

These variables override the corresponding config keys without editing any file:

| Variable | Config key | Example |
|---|---|---|
| `VP_CONTAINER_RUNTIME` | `container_runtime` | `VP_CONTAINER_RUNTIME=podman` |
| `VP_DEFAULT_AGENT` | `default_agent` | `VP_DEFAULT_AGENT=gemini` |
| `VP_AUTO_PULL` | `auto_pull` | `VP_AUTO_PULL=true` |
| `VP_LOG_LEVEL` | `log_level` | `VP_LOG_LEVEL=debug` |
| `VP_NO_COLOR` | `no_color` | `VP_NO_COLOR=true` |
| `VP_DATASETTE_PORT` | `logging.ui_port` | `VP_DATASETTE_PORT=9001` |
| `VP_PROXY_ENABLED` | `proxy.enabled` | `VP_PROXY_ENABLED=false` |
| `VP_CONFIG_DIR` | *(config root)* | `VP_CONFIG_DIR=/custom/path` |

### Image overrides

Each agent image can be overridden individually:

| Variable | Agent |
|---|---|
| `VP_IMAGE_CLAUDE` | claude |
| `VP_IMAGE_GEMINI` | gemini |
| `VP_IMAGE_OPENCODE` | opencode |
| `VP_IMAGE_DEVSTRAL` | devstral |
| `VP_IMAGE_AUGGIE` | auggie |
| `VP_IMAGE_COPILOT` | copilot |
| `VP_IMAGE_CODEX` | codex |
| `VP_DATASETTE_IMAGE` | datasette (logs UI) |
| `VP_PROXY_IMAGE` | proxy |

Set `VP_IMAGE_NAMESPACE` to change the prefix for all default images at once:

```bash
VP_IMAGE_NAMESPACE=myorg vp run claude
# pulls myorg/claude:latest
```

For end-to-end examples (extending a base image and assigning a brand-new image to an agent), see [Agents > Image customization workflows](agents/index.md#image-customization-workflows).

## Project-level config

Use `vp config init` in your repository to create `.vibepod/config.yaml` automatically when it does not already exist:

```bash
vp config init
```

If `.vibepod/config.yaml` already exists, this command exits without modifying it; run `vp config init --force` to replace the file.

This writes a minimal starter file:

```yaml
version: 1
```

To copy a full agent block into the project config, pass the agent name:

```bash
vp config init claude
```

This adds `agents.claude` to `.vibepod/config.yaml` (creating the file if needed). If `agents.claude` is already present, the command exits without modifying the file.

Then add only the keys you want to override. Project config is merged on top of global config and defaults.

```yaml
# .vibepod/config.yaml
default_agent: opencode
agents:
  opencode:
    env:
      OPENAI_BASE_URL: https://my-internal-proxy/v1
    init:
      - apt-get update
      - apt-get install -y ripgrep
```

`agents.<agent>.init` runs inside the container before the agent process starts. Commands run with `/bin/sh -lc` and `set -e` (startup stops on the first failed command).

Commit this file to share project defaults with your team.

## The built-in proxy

VibePod starts a `vibepod-proxy` container alongside every agent. It acts as an HTTP(S) MITM proxy and logs all outbound requests to a SQLite database viewable in the Datasette UI (`vp logs start`).

The proxy is reachable inside the Docker network as `http://vibepod-proxy:8080`. It is not published on a host port.

To disable the proxy globally:

```yaml
proxy:
  enabled: false
```

Or at runtime:

```bash
VP_PROXY_ENABLED=false vp run claude
```
