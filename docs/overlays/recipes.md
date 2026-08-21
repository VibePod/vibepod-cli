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

## Contributing a recipe

Got an overlay other projects could reuse? Open an issue or PR with the
fragment and a line on what it is for — this page is meant to grow from
real-world use.
