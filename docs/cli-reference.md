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

::: mkdocs-typer
    :module: vibepod.cli
    :command: app
    :depth: 2
