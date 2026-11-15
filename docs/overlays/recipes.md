# Overlay recipes

Ready-to-copy overlay fragments for common needs. Each recipe is a `FROM`-less
Dockerfile: drop it into `.vibepod/overlay/Dockerfile` to apply it to every
agent in the project, or into `.vibepod/overlay/<agent>/Dockerfile` for a
single agent. See the [overview](index.md) for how overlays are built and
cached.

Fragments compose — a single `Dockerfile` can combine several recipes.

## apt packages

System packages from the distribution repositories. The smallest useful
overlay.

```dockerfile
# .vibepod/overlay/Dockerfile — no FROM line
RUN apt-get update && apt-get install -y --no-install-recommends \
        jq ripgrep sqlite3 \
    && rm -rf /var/lib/apt/lists/*
```

- `--no-install-recommends` and the `apt` list cleanup keep the overlay image
  small.
- Agent base images are Debian-based; swap in `apk`/`dnf` only if you have
  overridden the base image with something else.

## Python requirements

The overlay directory is the docker build context, so a fragment can `COPY`
files committed next to it.

```text
.vibepod/overlay/
├── Dockerfile
└── requirements.txt
```

```dockerfile
# .vibepod/overlay/Dockerfile — no FROM line
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
```

Editing `requirements.txt` changes the overlay hash, so the image rebuilds on
the next run — no manual invalidation needed.

## pixi package manager

[pixi](https://pixi.sh) gives agents on-demand access to the conda-forge
ecosystem — PDF tools, image libraries, scientific stacks — without root or a
rebuild at runtime (`pixi init && pixi add poppler`). From
[issue #140](https://github.com/VibePod/vibepod-cli/issues/140).

```dockerfile
# .vibepod/overlay/Dockerfile — no FROM line
ADD https://github.com/prefix-dev/pixi/releases/download/v0.76.2/pixi-x86_64-unknown-linux-musl.tar.gz /tmp/pixi.tar.gz
RUN mkdir -p /opt/pixi/bin && tar -xzf /tmp/pixi.tar.gz -C /opt/pixi/bin && rm /tmp/pixi.tar.gz
ENV PATH="/opt/pixi/bin:${PATH}"
```

- **`ADD` from URL instead of `curl | bash`** — works even when the base image
  ships no curl.
- **Fixed path `/opt/pixi` instead of `$HOME/.pixi`** — several agents
  override `HOME` at runtime, so a build-time `~` would not resolve to the
  same place. `ENV PATH` persists in the image config regardless of `HOME`.
- **Pinned version** — with `latest`, the overlay cache keeps whatever pixi
  version the first build downloaded until the fragment text changes.
- **Architecture** — the URL above is x86_64; on an arm64 host (Apple
  silicon), use the `pixi-aarch64-unknown-linux-musl.tar.gz` tarball. For a
  fragment that works on both, pick the tarball at build time with `uname -m`
  (matches pixi's tarball naming; requires curl in the base image). Overlays
  build with the classic builder, so BuildKit's `TARGETARCH` arg is not
  available:

    ```dockerfile
    RUN mkdir -p /opt/pixi/bin && \
        curl -fsSL "https://github.com/prefix-dev/pixi/releases/download/v0.76.2/pixi-$(uname -m)-unknown-linux-musl.tar.gz" \
        | tar -xz -C /opt/pixi/bin
    ENV PATH="/opt/pixi/bin:${PATH}"
    ```

## pixi + PDF/OCR toolchain

A real-world fragment by [@ReimarBauer](https://github.com/ReimarBauer) from
[issue #140](https://github.com/VibePod/vibepod-cli/issues/140): pixi with
checksum verification, plus a pre-built environment with poppler, qpdf,
ghostscript, and tesseract for PDF and OCR work. Described in his
[blog post on local AI-assisted PDF processing](https://www.fz-juelich.de/de/blogs/programmiere/lokale-ki-gestuetzte-pdf-verarbeitung-vibepod-pi-lm-studio-und-pixi).

```dockerfile
# .vibepod/overlay/Dockerfile — no FROM line
ENV PIXI_HOME=/opt/pixi

# Download pixi binary + official SHA256 checksum via Docker ADD (no curl/wget needed)
ADD https://github.com/prefix-dev/pixi/releases/latest/download/pixi-aarch64-unknown-linux-musl.tar.gz /tmp/pixi.tar.gz
ADD https://github.com/prefix-dev/pixi/releases/latest/download/pixi-aarch64-unknown-linux-musl.tar.gz.sha256 /tmp/pixi.sha256

# Verify checksum with sha256sum -c, then extract to $PIXI_HOME/bin
RUN set -ex && \
    cd /tmp && \
    sed 's|pixi-aarch64-unknown-linux-musl.tar.gz|/tmp/pixi.tar.gz|g' pixi.sha256 | sha256sum -c - && \
    mkdir -p ${PIXI_HOME}/bin && \
    tar --no-same-owner -xzf /tmp/pixi.tar.gz -C ${PIXI_HOME}/bin && \
    chmod +x ${PIXI_HOME}/bin/pixi && \
    rm -f /tmp/pixi.tar.gz /tmp/pixi.sha256

ENV PATH=/opt/pixi/bin:$PATH
RUN pixi --version

# Create vp_pixi project at /opt/vp_pixi (/opt to avoid overwriting user pixi installations)
RUN mkdir -p /opt/vp_pixi
WORKDIR /opt/vp_pixi
RUN pixi init

# Install PDF tools + OCR: poppler, qpdf, ghostscript, tesseract (all with checksum verification)
RUN pixi add poppler qpdf ghostscript tesseract

ENV PATH=/opt/vp_pixi/.pixi/envs/default/bin:$PATH

# Keep workspace environments off the bind-mounted `.pixi`, which the host may
# have populated for a different platform.
# The directory is named after `uname -m`, so each architecture gets its own
# tree (/workspace/.pixi-linux-aarch64, /workspace/.pixi-linux-x86_64).
RUN printf 'detached-environments = "/workspace/.pixi-linux-%s"\n' "$(uname -m)" \
    > ${PIXI_HOME}/config.toml && \
    cat ${PIXI_HOME}/config.toml

# `pixi global install` writes into PIXI_HOME and PIXI_HOME/bin, so the
# harness user needs write access to both. The sticky bit stops that user from
# replacing or deleting root's files there (bin/pixi stays root-owned, 755).
# Only the directories change, so this layer doesn't copy the pixi binary.
RUN chmod 1777 ${PIXI_HOME} ${PIXI_HOME}/bin
```

- Targets an arm64 host; on x86_64 replace `aarch64` with `x86_64` in both
  `ADD` URLs and the `sed` pattern.
- Uses `latest`; pin a release (as in the previous recipe) for
  reproducibility.
- The `detached-environments` setting is an example. It is global, so a
  project's own `.pixi/config.toml` overrides it; for a new project it is a
  starting point. That file is in the bind-mounted `.pixi`, so the container
  reads it too: with `detached-environments = false` there, pixi in the
  container installs into the host's `.pixi/envs` and can overwrite the host
  environments.
- The environments live in the workspace: add `.pixi-linux-*/` to the
  project's `.gitignore`.

## pi + MCP servers (pi-mcp-adapter)

[pi-mcp-adapter](https://github.com/nicobailon/pi-mcp-adapter) is the pi
extension that gives it MCP tools. The usual `pi install npm:pi-mcp-adapter`
does not survive as a build step: pi writes into `$HOME/.pi`, and `$HOME`
(`/config`) is a host mount at runtime, so anything installed there at build
time is shadowed. Install into `/opt` instead and point the project's
`.pi/settings.json` at it as a local-path package.

```dockerfile
# .vibepod/overlay/pi/Dockerfile — no FROM line
#
# $HOME (/config) is a host mount at runtime, so `pi install` at build time
# would be shadowed. Install into /opt instead; the project's
# .pi/settings.json references it as a local-path package.
ARG PI_MCP_ADAPTER_VERSION=2.34.0

RUN node -e 'const [a,b]=process.versions.node.split(".").map(Number); if (a<22||(a===22&&b<18)) { console.error("pi-mcp-adapter needs Node >= 22.18, got "+process.version); process.exit(1) }' \
    && mkdir -p /opt/pi-packages \
    && cd /opt/pi-packages \
    && npm init -y >/dev/null \
    && npm install --omit=dev --no-audit --no-fund "pi-mcp-adapter@${PI_MCP_ADAPTER_VERSION}" \
    && npm cache clean --force \
    && chmod -R a+rX /opt/pi-packages
```

Commit the two project files pi reads at startup. `.pi/settings.json` loads
the baked-in extension:

```json
{
  "packages": ["/opt/pi-packages/node_modules/pi-mcp-adapter"]
}
```

`.mcp.json` lists the servers to expose (shared with other MCP-aware tools):

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem@2026.8.31", "/workspace"]
    }
  }
}
```

- **Per-agent overlay** — the fragment lives under `.vibepod/overlay/pi/` so
  other agents in the project do not pay for an extension only pi loads.
- **Absolute path in `settings.json`** — pi resolves relative package paths
  against the settings file, which sits in the workspace mount; the install is
  outside it, so the path must be absolute.
- **`chmod -R a+rX`** — the overlay builds as root but pi runs as the
  container user; without it the extension is unreadable at runtime.
- **Node check** — the adapter's `engines` field says Node 20, but parts of
  it (the token commands, for one) need 22.18+; failing the build early beats
  a silent load failure in pi. Drop the guard if your base image is known-good.
- **Pinned version via `ARG`** — bump `PI_MCP_ADAPTER_VERSION` to upgrade;
  the fragment text changes, so the overlay rebuilds.
- **Pinned server version in `.mcp.json`** — `npx -y` fetches the package
  on first use, so an unpinned name resolves to whatever is latest that day
  and needs registry access at runtime. Pin it, or bake the server into the
  overlay next to the adapter and point `command` at it.

## pi + vibepod-board (OAuth sign-in)

pi's built-in MCP support can sign in to
[vibepod-board](https://github.com/VibePod/vibepod-board) through the browser,
so no board token has to be pasted into a config file. OAuth asks for two
things a container does not offer on its own: pi only signs in to a server on
HTTPS or `localhost`, and the browser on the host has to reach the callback
listener pi opens on the container's loopback interface. Two `socat` forwards
close both gaps. Starting them from the image entrypoint keeps the project
free of `agents.pi.init` configuration.

```dockerfile
# .vibepod/overlay/pi/Dockerfile — no FROM line
RUN apt-get update && apt-get install -y --no-install-recommends socat \
    && rm -rf /var/lib/apt/lists/*

# Forward localhost:3000 to the board and port 8766 to pi's OAuth callback
# listener on 127.0.0.1:8765, then hand over to the image's own entrypoint.
RUN printf '%s\n' '#!/bin/sh' \
    'socat TCP-LISTEN:3000,bind=127.0.0.1,fork,reuseaddr TCP:vibepod-board:3000 &' \
    'socat TCP-LISTEN:8766,fork,reuseaddr TCP:127.0.0.1:8765 &' \
    'exec /usr/local/bin/entrypoint.sh "$@"' > /usr/local/bin/board-forward.sh \
    && chmod +x /usr/local/bin/board-forward.sh
ENTRYPOINT ["/usr/local/bin/board-forward.sh"]
CMD ["pi"]
```

Commit the server entry pi reads at startup, `.pi/mcp.json`:

```json
{
  "mcpServers": {
    "vibepod-board": {
      "url": "http://localhost:3000/mcp",
      "oauth": { "callbackPort": 8765 }
    }
  }
}
```

Sign in once, publishing the callback port for that run:

```bash
vp run pi -p 127.0.0.1:8765:8766
```

Trust the project when pi asks, run `/mcp login vibepod-board`, open the
sign-in URL in the browser on the host, and pick the board projects pi may
use. pi stores the credentials in its config directory, which VibePod keeps
between runs, so from then on a plain `vp run pi` starts with the board
connected.

- **`localhost:3000` inside the container** — pi refuses OAuth over plain
  HTTP unless the server is on a loopback address, and the board accepts no
  other non-HTTPS `PUBLIC_URL`. The first forward makes the board's default
  address, `http://localhost:3000`, work from inside the container too.
- **Port 8766** — pi's callback listener binds `127.0.0.1:8765` in the
  container, where a published port cannot reach it. The second forward
  listens on 8766 on all interfaces and relays to it;
  `-p 127.0.0.1:8765:8766` maps it back to the port the browser is redirected
  to, on the host's loopback only. The port is needed for the sign-in run
  alone.
- **Entrypoint wrapper instead of `init`** — `agents.pi.init` could start the
  same forwards, but needs a `.vibepod/config.yaml` next to the overlay. The
  wrapper `exec`s the image's own entrypoint, so UID mapping still runs.
  `CMD` is repeated because setting `ENTRYPOINT` clears the inherited one.
- **Per-agent overlay** — the wrapper calls the pi image's entrypoint by
  path, and other agents in the project do not need the forwards.
- **Board address** — the forward targets `vibepod-board:3000`, the alias and
  port from the board's `compose.yml` on the
  [VibePod network](../integrations.md#services-on-the-vibepod-network).
  Adjust it if your board runs elsewhere. The board needs a version with
  OAuth sign-in.
- **Not together with pi-mcp-adapter** — the adapter from the previous recipe
  replaces pi's built-in MCP support, which reads `.pi/mcp.json` and provides
  `/mcp login`.
- **Token instead of OAuth** — pi can send a board API token as an
  `Authorization` header to `http://vibepod-board:3000/mcp` directly; that
  needs no overlay at all.

## Agent tooling: RTK and Graphify

Fragments that install [RTK](https://github.com/rtk-ai/rtk) and
[Graphify](https://github.com/Graphify-Labs/graphify) into the agent image —
together with the hook or skill registration they need — live on the
[External tools](../integrations.md#tools-inside-the-agent-container) page.

## Contributing a recipe

Got an overlay other projects could reuse? Open an issue or PR with the
fragment and a line on what it is for — this page is meant to grow from
real-world use.
