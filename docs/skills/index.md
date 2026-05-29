# Skills

Skills are reusable prompt/recipe folders that agents can discover at runtime.
Each skill is a directory with a `SKILL.md` file plus any supporting files it
needs.

`vp skills` installs, lists, updates, and removes skills through the
`vibepod-skills-engine` container, so you don't need Node, npm, pnpm, git, or
skill-specific tooling installed locally.

Browse example skills and future VibePod-specific skills in the
[`VibePod/vibepod-skills`](https://github.com/VibePod/vibepod-skills)
repository.

## Quick start

```bash
# install a skill globally for your user
vp skills add github:vibepod/vibepod-skills//skills/researcher --scope user

# paste a GitHub tree URL directly
vp skills add https://github.com/org/repo/tree/main/skills/researcher --scope user

# install into the current project instead
vp skills add github:vibepod/vibepod-skills//skills/researcher --scope local

# show what's installed across local + user scopes
vp skills list

# remove it
vp skills delete researcher --scope user
```

## Scopes

| Scope    | Lives in                                              | Default when                                       |
|----------|-------------------------------------------------------|----------------------------------------------------|
| `local`  | `<project>/.vibepod/skills/`                          | invoked inside a project (`.vibepod/` present)     |
| `user`   | `${XDG_CONFIG_HOME:-~/.config}/vibepod/skills/`        | invoked outside any project                        |

If you want a project install, pass `--scope local` explicitly. This creates
`<project>/.vibepod/skills/` when needed. Without `--scope local`, a command run
outside a directory tree that already contains `.vibepod/` defaults to `user`.

Local skills shadow user skills when the same ID is installed in both.
`vp skills list` shows which one wins.

## Locator examples

Use canonical locators when you know them:

```bash
vp skills add github:org/repo//skills/researcher#v1.0.0
vp skills add gitlab:org/repo//skills/sql#main
vp skills add npm:@acme/vibepod-skill-researcher
vp skills add ./skills/my-skill --link --scope local
```

For GitHub, you can also paste the browser URL for a folder:

```bash
vp skills add https://github.com/org/repo/tree/main/skills/researcher
```

See [Locator format](locators.md) for the full grammar, bundle installs, and
reproducibility details.

For concrete examples, see the
[`VibePod/vibepod-skills`](https://github.com/VibePod/vibepod-skills)
repository.

## Example skill repositories

These repositories are useful places to browse existing skills and authoring
patterns. Third-party skills can contain instructions and executable helper
scripts, so review the source and license before installing them.

- [`VibePod/vibepod-skills`][vibepod-skills] — VibePod examples and
  future VibePod-specific skills.
- [`anthropics/skills`][anthropic-skills] — Anthropic's public Agent Skills
  examples and templates.
- [`anthropics/claude-plugins-official`][anthropic-plugins] — official
  Claude Code plugins that include nested skills.
- [`openai/skills`][openai-skills] — Codex skills catalog using the same
  `SKILL.md` pattern.
- [`microsoft/skills`][microsoft-skills] — Microsoft and Azure SDK skills for
  coding agents.
- [`addyosmani/agent-skills`][addy-agent-skills] — production engineering
  workflow skills.
- [`obra/superpowers`][obra-superpowers] — agentic software development
  methodology skills.
- [`alirezarezvani/claude-skills`][alireza-claude-skills] — large multi-domain
  Claude/agent skills catalog.
- [`jezweb/claude-skills`][jezweb-claude-skills] — Claude Code plugin skills
  for web/product workflows.
- [`ericgandrade/claude-superskills`][claude-superskills] — universal AI
  skills for planning, research, and content.

Example installs from external repositories:

```bash
vp skills add github:anthropics/skills//skills/claude-api --scope user
vp skills add github:addyosmani/agent-skills//skills/code-review-and-quality --scope user
vp skills add \
  https://github.com/obra/superpowers/tree/main/skills/systematic-debugging \
  --scope user
vp skills add \
  https://github.com/alirezarezvani/claude-skills/tree/main/product-team/skills/spec-to-repo \
  --scope user
```

[vibepod-skills]: https://github.com/VibePod/vibepod-skills
[anthropic-skills]: https://github.com/anthropics/skills
[anthropic-plugins]: https://github.com/anthropics/claude-plugins-official
[openai-skills]: https://github.com/openai/skills
[microsoft-skills]: https://github.com/microsoft/skills
[addy-agent-skills]: https://github.com/addyosmani/agent-skills
[obra-superpowers]: https://github.com/obra/superpowers
[alireza-claude-skills]: https://github.com/alirezarezvani/claude-skills
[jezweb-claude-skills]: https://github.com/jezweb/claude-skills
[claude-superskills]: https://github.com/ericgandrade/claude-superskills

## How agents see skills

When you run a supported agent, VibePod reads the local and user skill lockfiles,
merges them, and mounts each installed skill into that agent's skill discovery
directory. Local skills win over user skills with the same ID.

Current SKILL.md auto-discovery support:

| Agent | Skill mount target inside the container |
|-------|-----------------------------------------|
| `claude` | `/claude/skills/<id>` |
| `codex` | `/config/.agents/skills/<id>` |
| `opencode` | `/config/.agents/skills/<id>` |
| `auggie` | `/config/.agents/skills/<id>` |
| `pi` | `/config/.agents/skills/<id>` |

Other agents can still run normally, but VibePod does not currently mount
SKILL.md folders into an auto-discovery location for them.

## Commands

| Command                                  | What it does                                                |
|------------------------------------------|-------------------------------------------------------------|
| `vp skills add <locator> [--id] [--scope]` | Install a skill                                            |
| `vp skills list [--scope] [--json]`      | Show installed skills with shadowing                        |
| `vp skills delete <id> [--scope]`        | Uninstall a skill                                           |
| `vp skills sync [--scope]`               | Reconcile `installed/` with the lockfile (no re-resolve)    |
| `vp skills update [<id>] [--scope]`      | Re-resolve locators and rewrite the lockfile                |

All commands accept `--json` for machine-readable output. The host CLI is a thin
wrapper around the engine container — see
[`vibepod-skills-engine`](https://github.com/VibePod/vibepod-skills-engine) for
what runs inside.

Use `sync` when you want to restore the exact installed contents from the
lockfile. Use `update` when you want to re-resolve moving refs such as branches
or package ranges and rewrite the lockfile.

## Configuration

| Env var                       | Effect                                                                 |
|-------------------------------|------------------------------------------------------------------------|
| `VP_SKILLS_ENGINE_IMAGE`      | Override the engine image (defaults to `${VP_IMAGE_NAMESPACE}/skills-engine:latest`) |
| `VP_IMAGE_NAMESPACE`          | Used to derive the default engine image                                |
| `VIBEPOD_TRUSTED_SOURCES`     | Comma-separated locator prefixes; if set, all other locators are rejected |
