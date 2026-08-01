# Credential Profiles

Profiles let you keep multiple credential sets per agent and switch between them
at run time — for example a Claude subscription login, a separate API-key setup,
and an environment prepared for Ollama.

A profile only switches the **credential directories** that get mounted into the
agent container. Everything else (skills, allowed directories, proxy, logging)
stays shared. Environment variables such as `ANTHROPIC_API_KEY` are still
configured via `agents.<agent>.env` or `-e` flags — combine them with a profile
via a project config (see below).

## Layout

```text
~/.config/vibepod/
  agents/<agent>/                    # the built-in "default" profile
  profiles/<name>/agents/<agent>/    # named profiles
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
