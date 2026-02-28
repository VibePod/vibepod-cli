# Configuration

VibePod merges configuration from three sources in order, with each layer overriding the previous:

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

# Pull the latest image before every run (default: false)
auto_pull: false

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
    image: nezhar/claude-container:latest
    env: {}       # Extra environment variables passed to the container
    volumes: []   # Reserved for future use

  gemini:
    enabled: true
    image: nezhar/gemini-container:latest
    env: {}
    volumes: []

  opencode:
    enabled: true
    image: nezhar/opencode-cli:latest
    env: {}
    volumes: []

  devstral:
    enabled: true
    image: nezhar/devstral-cli:latest
    env: {}
    volumes: []

  auggie:
    enabled: true
    image: nezhar/auggie-cli:latest
    env: {}
    volumes: []

  copilot:
    enabled: true
    image: nezhar/copilot-cli:latest
    env: {}
    volumes: []

  codex:
    enabled: true
    image: nezhar/codex-cli:latest
    env: {}
    volumes: []

logging:
  enabled: true
  image: vibepod/datasette:latest
  db_path: ~/.config/vibepod/logs.db
  ui_port: 8001         # Port for the Datasette UI

proxy:
  enabled: true
  image: vibepod/proxy:latest
  port: 8080
  db_path: ~/.config/vibepod/proxy/proxy.db
  ca_dir: ~/.config/vibepod/proxy/mitmproxy
  ca_path: ~/.config/vibepod/proxy/mitmproxy/mitmproxy-ca-cert.pem
```

## Environment variables

These variables override the corresponding config keys without editing any file:

| Variable | Config key | Example |
|---|---|---|
| `VP_DEFAULT_AGENT` | `default_agent` | `VP_DEFAULT_AGENT=gemini` |
| `VP_AUTO_PULL` | `auto_pull` | `VP_AUTO_PULL=true` |
| `VP_LOG_LEVEL` | `log_level` | `VP_LOG_LEVEL=debug` |
| `VP_NO_COLOR` | `no_color` | `VP_NO_COLOR=true` |
| `VP_DATASETTE_PORT` | `logging.ui_port` | `VP_DATASETTE_PORT=9001` |
| `VP_PROXY_PORT` | `proxy.port` | `VP_PROXY_PORT=9090` |
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
# pulls myorg/claude-container:latest
```

## Project-level config

Create `.vibepod/config.yaml` in your repository to apply settings that only take effect inside that project. Only the keys you specify are merged — everything else falls through to the global config and defaults.

```yaml
# .vibepod/config.yaml
default_agent: opencode
agents:
  opencode:
    env:
      OPENAI_BASE_URL: https://my-internal-proxy/v1
```

Commit this file to share project defaults with your team.

## The built-in proxy

VibePod starts a `vibepod-proxy` container alongside every agent. It acts as an HTTP(S) MITM proxy and logs all outbound requests to a SQLite database viewable in the Datasette UI (`vp logs start`).

To disable the proxy globally:

```yaml
proxy:
  enabled: false
```

Or at runtime:

```bash
VP_PROXY_ENABLED=false vp run claude
```
