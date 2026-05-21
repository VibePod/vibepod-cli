# Skills

Skills are reusable prompt/recipe folders agents can opt into. `vp skills` manages them through the `vibepod-skills-engine` container, so you don't need Node, npm, pnpm, git, or skill-specific tooling installed locally.

## Quick start

```bash
# add the canonical researcher skill into your current project
vp skills add github:vibepod/vibepod-skills//skills/researcher

# show what's installed (local + user)
vp skills list

# remove it
vp skills delete researcher
```

## Scopes

| Scope    | Lives in                                              | Default when                                       |
|----------|-------------------------------------------------------|----------------------------------------------------|
| `local`  | `<project>/.vibepod/skills/`                          | invoked inside a project (`.vibepod/` present)     |
| `user`   | `${XDG_CONFIG_HOME:-~/.config}/vibepod/skills/`        | invoked outside any project                        |

Local skills shadow user skills when the same ID is installed in both — `vp skills list` shows which one wins.

## Commands

| Command                                  | What it does                                                |
|------------------------------------------|-------------------------------------------------------------|
| `vp skills add <locator> [--id] [--scope]` | Install a skill                                            |
| `vp skills list [--scope] [--json]`      | Show installed skills with shadowing                        |
| `vp skills delete <id> [--scope]`        | Uninstall a skill                                           |
| `vp skills sync [--scope]`               | Reconcile `installed/` with the lockfile (no re-resolve)    |
| `vp skills update [<id>] [--scope]`      | Re-resolve locators and rewrite the lockfile                |

All commands accept `--json` for machine-readable output. The host CLI is a thin wrapper around the engine container — see [`vibepod-skills-engine`](https://github.com/VibePod/vibepod-skills-engine) for what runs inside.

## Configuration

| Env var                       | Effect                                                                 |
|-------------------------------|------------------------------------------------------------------------|
| `VP_SKILLS_ENGINE_IMAGE`      | Override the engine image (defaults to `${VP_IMAGE_NAMESPACE}/skills-engine:latest`) |
| `VP_IMAGE_NAMESPACE`          | Used to derive the default engine image                                |
| `VIBEPOD_TRUSTED_SOURCES`     | Comma-separated locator prefixes; if set, all other locators are rejected |
