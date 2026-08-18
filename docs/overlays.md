# Project overlays

A project can extend the agent image it runs in — extra apt packages, language
toolchains, CLIs — by committing a `FROM`-less Dockerfile fragment:

```text
.vibepod/overlay/
├── Dockerfile          # shared: applies to every agent in this project
└── claude/
    └── Dockerfile      # per-agent: wins over the shared one for claude
```

On `vp run` (and `vp task create`), VibePod appends the fragment to the agent's
base image and builds a workspace-local image tagged
`localhost/vibepod/overlay-<agent>:<hash>` (the explicit `localhost/` registry
keeps Podman from resolving the name against `docker.io`). The hash covers the
base image, the fragment,
and every file in the overlay directory, so:

- unchanged overlays never rebuild — the cached image is reused instantly
- switching branches with different overlays just switches images
- pulling a newer base image rebuilds the overlay on top of it

The overlay directory is the docker build context, so the fragment can `COPY`
files committed next to it:

```dockerfile
# .vibepod/overlay/Dockerfile — no FROM line
RUN apt-get update && apt-get install -y --no-install-recommends jq \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt
```

Because the directory is committable, the whole team shares the environment.
Symlinks anywhere in the overlay directory — including a symlinked
`Dockerfile` — are ignored: their targets may not exist on a teammate's
machine or may point outside the committed project.

## Compared to `init` and custom images

- `agents.<agent>.init` re-runs shell commands on **every** start — good for
  tiny setup, slow for package installs.
- A custom image (`agents.<agent>.image`, see
  [Customizing agent images](agents/index.md#overriding-the-image)) is fully
  manual: write a complete Dockerfile, build, tag, configure.
- An overlay sits in between: persistent like a custom image, zero-ceremony
  like `init`.

## Controls

- `vp run <agent> --no-overlay` — skip the overlay for one run
- `vp run <agent> --rebuild-overlay` — force a rebuild, bypassing docker's
  layer cache so every instruction re-runs
- `agents.<agent>.overlay: false` in `.vibepod/config.yaml` or the global
  config — disable overlays for that agent

Superseded overlay images for the same project and agent are removed
automatically after each successful build.
