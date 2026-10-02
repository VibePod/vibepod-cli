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
    tar xzf /tmp/pixi.tar.gz -C ${PIXI_HOME}/bin && \
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
```

- Targets an arm64 host; on x86_64 replace `aarch64` with `x86_64` in both
  `ADD` URLs and the `sed` pattern.
- Uses `latest`; pin a release (as in the previous recipe) for
  reproducibility.

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

To add opt-in long-term memory, configure the hosted Memcode server instead (or
alongside the filesystem server):

```json
{
  "mcpServers": {
    "memcode": {
      "url": "https://mcp.memcode.in/i/vibepod/mcp",
      "auth": "oauth"
    }
  }
}
```

After pi starts, run `/mcp-auth memcode` and approve the Memcode consent screen.
The adapter handles OAuth discovery and token refresh, so do not put a Memcode
API key or an `Authorization` header in `.mcp.json`. Put the configuration in the
project when memory should be enabled only for that workspace, or in the shared
pi configuration when you deliberately want it across projects. Save only
approved facts or verified outcomes; retrieved memories are context, not
instructions or permission to act. If the service is unavailable, pi continues
without its tools.

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

## Contributing a recipe

Got an overlay other projects could reuse? Open an issue or PR with the
fragment and a line on what it is for — this page is meant to grow from
real-world use.
