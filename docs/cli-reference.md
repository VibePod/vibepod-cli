# CLI Reference

Complete reference for all `vp` commands, auto-generated from the source.

## Volume mappings

`vp run` and agent aliases accept repeatable `-v/--volume` options. A single
absolute POSIX host path mounts at the same path with read-write access:

```bash
vp run claude --volume /my/path
vp run claude --volume /my/path:/my/path
vp claude -v /my/path -v /host/data:/container/data:ro
```

Host paths must exist. Relative paths, `~` paths, and named volumes require an
explicit container destination. See [Mounting volumes](configuration.md#mounting-volumes)
for modes, configured volumes, and mount restrictions.

## Workspace at its host path

`vp run` always mounts the workspace at `/workspace`. Add
`--workspace-system-path` to also mount it at its absolute host path, for tools,
configs, and scripts that refer to absolute project paths:

```bash
cd /home/alice/projects/demo
vp run claude --workspace-system-path
# Inside the container, both paths show the same files:
#   /workspace
#   /home/alice/projects/demo
```

The second mount is equivalent to `--volume "$PWD:$PWD"` (with `-w DIR`, to
`DIR:DIR`), uses the same read-write mode and SELinux handling as `/workspace`,
and works alongside other `-v/--volume` options. The container still starts in
`/workspace`. When the workspace path contains a symlink, both the resolved and
the given path are mounted.

The host path must be an absolute POSIX path; on Windows, run `vp` inside WSL2.
VibePod rejects paths that would hide container paths it manages, such as
`/config`, `/etc`, `/usr`, `/tmp`, or an agent's install directory, and a
`--volume` whose target is the workspace host path. In `--acp` mode the
workspace is already mounted at its host path, so the option has no effect.

::: mkdocs-typer
    :module: vibepod.cli
    :command: app
    :depth: 2
