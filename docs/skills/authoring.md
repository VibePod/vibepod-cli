# Authoring a skill

A skill is a folder with a `SKILL.md`. Everything else is optional.

## SKILL.md

```markdown
---
name: Researcher
version: 0.1.0
description: Investigate a topic and produce a source-cited brief.
tags: [research, summarization]
permissions: [read_workspace, net_read]
---

# Researcher

You are a focused research assistant. ...
```

### Required frontmatter

- `name` — human-readable; slugified to the install ID.
- `description` — one sentence shown by `vp skills list`.

### Optional frontmatter

- `version` — semver, stored in the lock.
- `tags` — string array for discovery.
- `requires.tools` — external CLI tools the skill expects (`ffmpeg`, `git`, ...).
- `permissions` — capability hints (`read_workspace`, `write_workspace`, `net_read`, ...). Coarse, advisory.

### Body

Free-form markdown instructions. Must not be empty.

## Authoring loop

```bash
mkdir -p ./skills/my-skill
$EDITOR ./skills/my-skill/SKILL.md

# install as a symlink so edits show up immediately
vp skills add ./skills/my-skill --link --scope local

# iterate, then validate:
docker run --rm -v "$PWD/skills/my-skill:/in" \
  vibepod/skills-engine:latest validate /in
```

## Publishing

| Channel    | How                                                                  |
|------------|----------------------------------------------------------------------|
| GitHub     | Drop the skill folder anywhere; share `github:org/repo//path` locator |
| GitLab     | Same as GitHub with `gitlab:` prefix                                  |
| npm        | Publish as `vibepod-skill-<id>` or `@scope/vibepod-skill-<id>`        |

For GitHub, users can also paste a browser `tree` URL for the skill folder;
VibePod normalizes it to the canonical `github:org/repo//path#ref` locator.

For npm publication, the package root should be the skill folder itself (`SKILL.md` at the top level).
