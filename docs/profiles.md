# Credential Profiles

Profiles let you keep multiple credential sets per agent and switch between them
at run time — for example a Claude subscription login, a separate API-key setup,
and an environment prepared for Ollama.

A profile switches the **credential directories** that get mounted into the
agent container and, when the profile has its own filter settings, the
**proxy allow/deny filter** (see below). Everything else (skills, allowed
directories, proxy, logging) stays shared. Environment variables such as
`ANTHROPIC_API_KEY` are still configured via `agents.<agent>.env` or `-e`
flags — combine them with a profile via a project config (see below).

## Layout

```text
~/.config/vibepod/
  agents/<agent>/                    # the built-in "default" profile
  profiles/<name>/agents/<agent>/    # named profiles
  profiles/<name>/filter.yaml        # optional per-profile proxy filter
```

Your existing credentials in `~/.config/vibepod/agents/` are the `default`
profile — nothing moves when you start using profiles.

## Managing profiles

```bash
vp profile list            # list profiles; * marks the active one, agents with
                           # stored data (config, caches, credentials) in parentheses
vp profile create work     # create an empty profile
vp profile remove work     # delete a profile and its credentials (asks first)
```

Profile names are lowercase slugs: letters, digits, `-` and `_`.
The `default` profile always exists and cannot be removed.

## Using a profile

```bash
vp run claude --profile work
vp task create claude "summarize the diff" --profile work
vp doctor claude --profile work
```

The first run with a fresh profile starts unauthenticated — log in once and the
credentials are persisted inside that profile.

## Selecting a profile without the flag

Resolution order (first match wins):

1. `--profile` flag
2. `VP_PROFILE` environment variable
3. `profile:` key in the merged config (global `~/.config/vibepod/config.yaml`
   or project `.vibepod/config.yaml`)
4. `default`

Pinning a profile per project pairs well with per-project env vars:

```yaml
# .vibepod/config.yaml — a project wired to a local Ollama
version: 1
profile: ollama
agents:
  claude:
    env:
      ANTHROPIC_BASE_URL: http://host.docker.internal:11434
```

Referencing a profile that does not exist is a hard error — create it first
with `vp profile create <name>`.

## Per-profile proxy filter

Each named profile can carry its own proxy filter mode and allow/deny lists in
`profiles/<name>/filter.yaml`. The `vp proxy filter` commands act on the
active profile, or on an explicit one via `--profile`:

```bash
vp proxy filter mode allow --profile work
vp proxy filter allow add api.anthropic.com --profile work
vp proxy filter status --profile work
```

The file is created on first write, seeded from the global filter settings.
A profile without `filter.yaml` inherits the global `proxy.filter` config and
then any project filter captured for that launch. An explicit profile file is
a complete replacement for the global/project base. In both cases, the
launch-time `VP_PROXY_FILTER_MODE` value wins last.

The proxy keeps one materialized profile base and a small record for each
container. Editing a profile filter hot-reloads every running container that
uses it, while each container retains the project and environment overrides it
started with. This means two agents using different profiles—or different
projects with the same inherited profile—can safely share one proxy.

`vp list` shows the selected profile and live effective proxy mode for each
running container. Removing a profile deletes its credentials, but VibePod
retains its materialized filter while any existing container (including a
stopped container) still references it.
