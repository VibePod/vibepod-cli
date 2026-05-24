# Locator format

VibePod skill locators use one of three grammars depending on the source.

## Git-based (GitHub, GitLab, generic HTTPS Git)

```
<source>:<repo>[//<subpath>][#<ref>]
```

| Part        | Meaning                                                              | Example          |
|-------------|----------------------------------------------------------------------|------------------|
| `<source>`  | `github`, `gitlab`, or a full `https://...` URL                      | `github`         |
| `<repo>`    | `org/repo` for github/gitlab; `host/path.git` for generic            | `vibepod/vibepod-skills` |
| `//<subpath>` | optional path inside the repo                                      | `//skills/researcher` |
| `#<ref>`    | optional branch, tag, or commit (resolved commit goes in the lock)  | `#v1.0.0`        |

Examples:

```text
github:vibepod/vibepod-skills//skills/researcher#v1.0.0
gitlab:acme/agent-skills//skills/sql#main
https://git.example.com/org/repo.git//skills/foo#abc123
```

You can also paste common GitHub `tree` URLs; VibePod normalizes them to
the canonical `github:` locator before invoking the skills engine:

```text
https://github.com/org/repo/tree/main/skills/researcher
# becomes github:org/repo//skills/researcher#main
```

## npm

```
npm:<package>[@<version>]
```

`//subpath` and `#ref` do **not** apply — rely on the npm package's own version range.

Examples:

```text
npm:@acme/vibepod-skill-researcher
npm:@acme/vibepod-skill-researcher@1.2.0
```

The recommended naming convention for the curated channel is `vibepod-skill-<id>` or `@scope/vibepod-skill-<id>`, so it's greppable on the npm registry.

## Local

```
<relative-or-absolute-path>
```

There is no `file:` scheme — bare paths only. Distinguished from other sources by a leading `.` or `/`.

Examples:

```text
./skills/researcher
/abs/path/to/skill
```

Use `--link` for symlink installs while authoring a skill:

```bash
vp skills add ./skills/researcher --link
```

## Bundle install

If the resolved source has **no `SKILL.md`** at its root, the engine looks for skills in one of two places:

1. Immediate subdirectories that each contain a `SKILL.md`.
2. A conventional `skills/` subdirectory whose subdirectories each contain a `SKILL.md` — this is what repos like [`obra/superpowers`](https://github.com/obra/superpowers) ship.

In either case, all matching skills are installed in a single call. The fetch happens once; each skill ends up in the lockfile with its own expanded per-skill locator, so `vp skills update <id>` and `vp skills sync` work normally afterward.

```bash
# whole repo — finds skills/ and installs all 14
vp skills add github:obra/superpowers

# equivalent, explicit subpath
vp skills add github:obra/superpowers//skills

# one specific skill
vp skills add github:obra/superpowers//skills/test-driven-development
```

Directories without a `SKILL.md` (e.g. `docs/`, `assets/`, `tests/`) are skipped silently.

`--id` is rejected with bundle installs since it'd apply to all skills at once. npm bundles aren't supported (npm locators don't carry a subpath).

## Reproducibility

The registry (`skills.json`) records what you asked for; the lockfile (`skills-lock.json`) records what got resolved.

For git-based locators:

- `#<branch>` may move on `vp skills update`.
- `#<tag>` or `#<commit>` stay pinned.
- The lockfile's `source.locator` is **always** pinned to the resolved commit (`…#<sha>`), even if you installed without a `#ref` or pinned a branch. That guarantees `vp skills sync` restores the exact tree that was installed, regardless of where the upstream branch points later.
- The lockfile also keeps `source.commit` and the original `source.ref` as separate fields for inspection.

For npm locators the same rule applies: the lockfile pins `npm:<pkg>@<exact-resolved-version>` even when the registry tracks `npm:<pkg>` or a range like `npm:<pkg>@^1.0.0`.
