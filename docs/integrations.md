# Integrating external tools

VibePod agents run in containers, so plugging in third-party tooling comes
down to *where the tool lives*:

| Tool | What it does | Where it runs | Pattern |
|------|--------------|---------------|---------|
| [LiteLLM](https://github.com/BerriAI/litellm) | Model gateway: one endpoint and key for 100+ providers | Its own container | [Service on the VibePod network](#services-on-the-vibepod-network) |
| [Headroom](https://github.com/headroomlabs-ai/headroom) | Compression proxy: shrinks tool output, logs and files before they reach the model | Its own container | [Service on the VibePod network](#services-on-the-vibepod-network) |
| [RTK](https://github.com/rtk-ai/rtk) | Rewrites shell commands so the agent reads compact output | Inside the agent container | [Tool in the agent image](#tools-inside-the-agent-container) |
| [Graphify](https://github.com/Graphify-Labs/graphify) | Knowledge graph of the codebase the agent queries instead of grepping | Inside the agent container | [Tool in the agent image](#tools-inside-the-agent-container) |

Both patterns use configuration VibePod already has — no plugin or code change
is involved. This page describes each pattern once and then applies it to the
four tools from [issue #130](https://github.com/VibePod/vibepod-cli/issues/130).

## Services on the VibePod network

Every `vp run` creates (or reuses) the Docker network `vibepod-network` and
attaches the agent and the built-in `vibepod-proxy` container to it. The
fastest way to make a service reachable from agents is to start it on that
network under a stable alias — the same wiring
[vibepod-board](https://github.com/VibePod/vibepod-board/blob/main/compose.yml)
uses:

```yaml
# compose.yml of the tool you want to integrate
services:
  mytool:
    image: example/mytool:latest
    networks:
      vibepod:
        aliases:
          - mytool          # agents reach it as http://mytool:<port>

networks:
  vibepod:
    name: ${VIBEPOD_NETWORK:-vibepod-network}
    external: true
```

- `external: true` makes compose join the network instead of creating its
  own. The network has to exist first: run any agent once (`vp run claude`)
  or create it by hand with `docker network create vibepod-network`. If you
  changed the `network:` key in your VibePod config, tell the tool's compose
  file which one to join by setting `VIBEPOD_NETWORK` when you run
  `docker compose up` — it only drives the interpolation above; VibePod
  itself reads the `network:` config key.
- No `ports:` are needed. The service is reachable from containers on the
  network — agents and the proxy — but not from the host or the LAN. Publish a
  port only when you also want the tool's web UI in your browser.
- The alias is plain container DNS. On Podman this needs a DNS-enabled
  network; see [Quickstart — Using Podman](quickstart.md#using-podman-instead-of-docker).

### Pointing the agent at the service

Agents are configured through environment variables, so referencing the
service means setting the right variable to `http://<alias>:<port>`. Pick
the scope you need:

```yaml
# ~/.config/vibepod/config.yaml (global) or .vibepod/config.yaml (project)
agents:
  claude:
    env:
      ANTHROPIC_BASE_URL: http://mytool:4000
```

```bash
vp run claude -e ANTHROPIC_BASE_URL=http://mytool:4000   # this run only
```

For LLM gateways the [`llm:` section](llm.md) sets base URL, key and model
for every agent with an LLM mapping (Claude Code, Codex) in one place.

Some agents read endpoints from their own config files rather than the
environment (Codex's `config.toml`, Pi's `models.json`, Tau's
`catalog.toml`). Those files live in the agent's persisted config directory,
which is mounted into the container on every run:

```console
$ vp config path
Config:  /home/me/.config/vibepod
Global:  /home/me/.config/vibepod/config.yaml
Project: /home/me/project/.vibepod/config.yaml
Logs:    /home/me/.config/vibepod/logs.db
Proxy:   /home/me/.config/vibepod/proxy/proxy.db
```

`Config:` is the root. Below it, `agents/<agent>/` is the `default`
[profile](profiles.md) and `profiles/<name>/agents/<agent>/` a named one; the
container sees that directory as its config or home mount, so a file you edit
there is what the agent reads. Pinning a profile per project (`profile: work`
in `.vibepod/config.yaml`) keeps a "via gateway" credential set next to a
direct one.

### The built-in proxy

Agents send outbound HTTP through `vibepod-proxy`, and that includes requests
to a sidecar alias — calls to `http://litellm:4000` show up in `vp logs` like
any other traffic. Two consequences:

- In `allow` filter mode, allow the alias: `vp proxy filter allow add litellm`.
- To bypass the proxy for the sidecar (say, to compare latencies), extend
  `NO_PROXY`: `-e NO_PROXY=localhost,127.0.0.1,::1,litellm`.

### Tools with their own compose network

If a tool ships a compose file you would rather not edit, keep its network and
connect the agent to it instead:

```bash
vp run claude --network mytool_default
```

When the workspace itself contains a compose file, `vp run` offers this
interactively — see
[Connecting to a Docker Compose network](agents/index.md#connecting-to-a-docker-compose-network).

## LiteLLM

[LiteLLM](https://docs.litellm.ai/) is an OpenAI- and Anthropic-compatible
gateway: one endpoint, one key, any provider behind it, plus spend tracking,
virtual keys and fallbacks. It serves `/v1/messages`, so Claude Code talks to
it natively, and `/v1/chat/completions` and `/v1/responses` for OpenAI-style
clients.

**1. Start it on the VibePod network**

```yaml
# litellm/compose.yml
services:
  litellm:
    image: docker.litellm.ai/berriai/litellm:latest
    command: ["--config", "/app/config.yaml", "--port", "4000"]
    volumes:
      - ./config.yaml:/app/config.yaml:ro
    environment:
      LITELLM_MASTER_KEY: ${LITELLM_MASTER_KEY:?set a master key}
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:-}
      OPENAI_API_KEY: ${OPENAI_API_KEY:-}
    networks:
      vibepod:
        aliases:
          - litellm

networks:
  vibepod:
    name: ${VIBEPOD_NETWORK:-vibepod-network}
    external: true
```

```yaml
# litellm/config.yaml — model_name is what agents will ask for
model_list:
  - model_name: claude-sonnet-4-6
    litellm_params:
      model: anthropic/claude-sonnet-4-6
      api_key: os.environ/ANTHROPIC_API_KEY
  - model_name: gpt-5.3-codex
    litellm_params:
      model: openai/gpt-5.3-codex
      api_key: os.environ/OPENAI_API_KEY

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
```

```bash
cd litellm
LITELLM_MASTER_KEY=sk-litellm-... ANTHROPIC_API_KEY=sk-ant-... docker compose up -d
```

**2. Point agents at `http://litellm:4000`**

=== "Claude Code"

    The [`llm:` section](llm.md) covers Claude Code end to end: base URL, key
    (sent as `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`) and the model name
    from `model_list`.

    ```yaml
    # .vibepod/config.yaml
    llm:
      enabled: true
      base_url: http://litellm:4000
      api_key: sk-litellm-...      # master key or a LiteLLM virtual key
      model: claude-sonnet-4-6
    ```

    ```bash
    vp run claude -p "say ok"
    ```

=== "Codex"

    Codex selects providers in its `config.toml`, which lives in the
    persisted agent directory: `<Config>/agents/codex/.codex/config.toml`
    (create it if missing; `profiles/<name>/agents/codex/...` for a named
    profile).

    ```toml
    model = "gpt-5.3-codex"
    model_provider = "litellm"

    [model_providers.litellm]
    name = "litellm"
    base_url = "http://litellm:4000/v1"
    env_key = "LITELLM_API_KEY"
    ```

    Then hand Codex the key named in `env_key`:

    ```yaml
    # .vibepod/config.yaml
    agents:
      codex:
        env:
          LITELLM_API_KEY: sk-litellm-...
    ```

=== "Other agents"

    Any agent that accepts an OpenAI-compatible base URL works the same way,
    e.g. Qwen Code with `OPENAI_BASE_URL: http://litellm:4000/v1` in
    `agents.qwen.env`. Agents with their own provider files are covered in
    [Agents](agents/index.md): Pi's `models.json`, Tau's `catalog.toml`,
    Jcode's `config.toml`.

LiteLLM's [Claude Code](https://docs.litellm.ai/docs/tutorials/claude_responses_api)
and [Codex](https://docs.litellm.ai/docs/tutorials/openai_codex) guides cover
the agent side in more depth.

## Headroom

[Headroom](https://docs.headroomlabs.ai/docs) is a compression proxy between
the agent and the provider: it shrinks tool output, logs and file contents in
the prompt before they reach the model, and keeps the originals retrievable.
Its `headroom wrap` command is a host-side workflow (start a local proxy,
launch the agent); inside VibePod you run its plain proxy mode and let the
container network do the wiring.

**1. Start it on the VibePod network**

```yaml
# headroom/compose.yml
services:
  headroom:
    # The image's entrypoint is `headroom proxy`, listening on 0.0.0.0:8787.
    image: ghcr.io/headroomlabs-ai/headroom:latest
    environment:
      HOME: /home/nonroot
      HEADROOM_WORKSPACE_DIR: /home/nonroot/.headroom
      HEADROOM_CONFIG_DIR: /home/nonroot/.headroom/config
      # HEADROOM_OUTPUT_SHAPER: "1"   # also trim what the model writes back
    volumes:
      - headroom-state:/home/nonroot/.headroom   # savings ledger, CCR cache, logs
    networks:
      vibepod:
        aliases:
          - headroom

volumes:
  headroom-state:

networks:
  vibepod:
    name: ${VIBEPOD_NETWORK:-vibepod-network}
    external: true
```

Headroom forwards the credentials the agent sends, so the proxy needs no
provider key of its own: Claude Code keeps using its login (API key or
subscription token). Leave port 8787 unpublished — on the VibePod network only
agents can reach it. If you publish it anyway, set `HEADROOM_PROXY_TOKEN` as
Headroom's own compose file requires.

**2. Point agents at it**

```yaml
# .vibepod/config.yaml
agents:
  claude:
    env:
      ANTHROPIC_BASE_URL: http://headroom:8787
```

OpenAI-style clients use `http://headroom:8787/v1`; for Codex put that in a
`model_providers` block as shown for [LiteLLM](#litellm).

**3. Watch the savings**

Compression happens after the VibePod proxy has seen the request, so
`vp logs` reports the uncompressed sizes. Headroom's own numbers live in its
container:

```bash
docker compose -f headroom/compose.yml exec headroom headroom savings
```

To chain Headroom in front of LiteLLM, point Headroom's upstream at the
gateway (`ANTHROPIC_TARGET_API_URL: http://litellm:4000` in its environment)
and the agents at Headroom.

## Tools inside the agent container

RTK and Graphify are not services. They are programs the agent invokes, plus
a hook or skill that tells the agent to use them. Two VibePod features cover
that:

- A [project overlay](overlays/index.md) installs the binary into the agent
  image once — content-addressed, cached, shared with the team through the
  committed `.vibepod/overlay/` directory.
- The registration step writes into the agent's persisted config dir. For
  Claude Code the container sets `CLAUDE_CONFIG_DIR=/claude`, which is the
  host's `agents/claude/` directory (see `vp config path`); both RTK and
  Graphify honor that variable, so a hook or skill registered once is there
  on every later run and in every project.

Run the registration **inside a session** so the files belong to your user:
Claude Code's bash mode executes a line starting with `!` in the container as
the agent user. `agents.<agent>.init` can automate it, but the init wrapper
replaces the image entrypoint, so the user-switching step is bypassed: init
commands (and the agent that follows them) run as root, and on Linux the
files they create in the config dir end up root-owned.

## RTK

[RTK](https://github.com/rtk-ai/rtk) rewrites the shell commands an agent
runs (`git status` → `rtk git status`, `pytest` → `rtk pytest`, …) and returns
compact output — up to 90 % less bash output for the agent to read.

**1. Install the binary via an overlay**

```dockerfile
# .vibepod/overlay/Dockerfile — no FROM line
ADD https://github.com/rtk-ai/rtk/releases/download/v0.48.0/rtk-x86_64-unknown-linux-musl.tar.gz /tmp/rtk.tar.gz
RUN tar -xzf /tmp/rtk.tar.gz -C /usr/local/bin rtk \
    && chmod 755 /usr/local/bin/rtk \
    && rm /tmp/rtk.tar.gz
```

- On an arm64 host (Apple silicon) use `rtk-aarch64-unknown-linux-gnu.tar.gz`
  — note the different libc suffix.
- Pin the release: with a moving URL the overlay cache would keep whatever it
  downloaded first.
- Some RTK filters shell out to ripgrep; the `vibepod/claude` image ships it.

**2. Register the hook once**

In a running `vp run claude` session:

```text
! rtk init -g --auto-patch
```

`--auto-patch` skips the consent prompt. RTK adds a `PreToolUse` hook to
`/claude/settings.json` and a short `RTK.md` next to it; restart the agent to
load the hook. RTK's other integrations (`rtk init -g --codex`, `--gemini`,
`--opencode`, `--agent pi`, `--agent vibe`) follow the same shape — the
[supported agents table](https://github.com/rtk-ai/rtk#supported-ai-tools)
lists the mechanism each one uses.

The hook covers Bash tool calls only; Claude Code's built-in `Read`, `Grep`
and `Glob` tools bypass it. RTK's savings statistics (`rtk gain`) live in the
container user's home, which is not persisted — the hook is.

## Graphify

[Graphify](https://github.com/Graphify-Labs/graphify) parses the codebase
into a knowledge graph (`graphify-out/` in the workspace) that the agent
queries instead of grepping: `/graphify .` builds it; `graphify query`,
`path` and `explain` answer questions against it. The Python package is
`graphifyy` (double y).

**1. Install the CLI via an overlay**

```dockerfile
# .vibepod/overlay/claude/Dockerfile — no FROM line
RUN apt-get update && apt-get install -y --no-install-recommends python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/graphify \
    && /opt/graphify/bin/pip install --no-cache-dir graphifyy \
    && ln -s /opt/graphify/bin/graphify /usr/local/bin/graphify
```

The `vibepod/claude` image ships `python3` but no `pip`, hence the venv. A
fixed path under `/opt` keeps the interpreter location stable regardless of
the `HOME` the agent runs with — the skill records that path in
`graphify-out/` and reuses it later.

**2. Register the skill**

Once per config dir, in a running `vp run claude` session:

```text
! graphify install
```

This writes `/claude/skills/graphify/SKILL.md` and a pointer in
`/claude/CLAUDE.md`. To ship the skill with the project instead, run
`graphify install --project` in the workspace (from the session, or on the
host with `pipx install graphifyy`); it writes `.claude/skills/graphify/` for
you to commit. For Codex use `graphify install --platform codex`, which
targets `~/.codex` — the persisted `agents/codex/` mount.

Then, in the agent:

```text
/graphify .
```

## Verifying the wiring

```bash
docker network inspect vibepod-network --format '{{range .Containers}}{{.Name}} {{end}}'
vp config show            # merged config: env, llm and init sections
vp logs start             # requests to a sidecar alias appear like any other traffic
```
