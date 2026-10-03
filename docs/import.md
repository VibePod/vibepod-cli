# Importing an Existing Agent Setup

Already running Claude Code, opencode, Codex or another supported agent
directly on your machine? `vp import` copies that configuration into a VibePod
profile, mapped to the paths the agent reads **inside its container** — so your
settings, custom models, hooks, skills and memory files come along instead of
being set up a second time by hand.

The same command copies one profile's agent configuration into another profile.

## Quick start

```bash
vp import                        # scan your home directory for installed agents
vp import claude --help-agent    # exactly which files this agent would copy
vp import claude --dry-run       # resolve the plan, write nothing
vp import claude                 # copy into the active profile
```

## What gets copied

Files are classified into categories. Six are copied by default; three are
opt-in:

| Category      | Copied            | What it holds                                              |
| ------------- | ----------------- | ---------------------------------------------------------- |
| `settings`    | by default        | the agent's main config file                                |
| `models`      | by default        | custom model and provider definitions                       |
| `mcp`         | by default        | MCP server definitions                                      |
| `hooks`       | by default        | hooks and plugins that run around the agent                 |
| `skills`      | by default        | skills, commands, subagents, prompts                        |
| `memory`      | by default        | user-level memory files (`CLAUDE.md`, `AGENTS.md`, …)       |
| `credentials` | `--with-credentials` | tokens, API keys, OAuth material                         |
| `sessions`    | `--with-sessions` | transcripts, history, caches, telemetry                     |
| `other`       | `--with-other`    | files no category claims yet                                |

Narrow or widen the selection per run:

```bash
vp import claude --only skills,hooks
vp import claude --skip memory
```

`vp import <agent> --help-agent` prints the real source → destination paths for
that agent, which categories they belong to, and which flags opt the rest in.

Anything under an agent's config directory that no category claims is **not
copied silently** — the run reports it, and `--with-other` includes it at the
same relative path. New upstream releases add files; you see them rather than
losing them. A secret-looking file (see [Credentials](#credentials)) still
needs `--with-credentials` as well.

## Per-agent sources

| Agent      | Read from                                              |
| ---------- | ------------------------------------------------------ |
| `claude`   | `~/.claude`                                            |
| `gemini`   | `~/.gemini`                                            |
| `opencode` | `~/.config/opencode`, `~/.local/share/opencode`        |
| `devstral` | `~/.config/mistral`                                    |
| `auggie`   | `~/.augment`                                           |
| `copilot`  | `~/.copilot`                                           |
| `codex`    | `~/.codex`                                             |
| `pi`       | `~/.pi`                                                |
| `agy`      | `~/.agy`                                               |
| `tau`      | `~/.tau`                                               |
| `jcode`    | `~/.jcode`, `~/.config/jcode`                          |
| `freebuff` | `~/.config/manicode`                                   |
| `qwen`     | `~/.qwen`                                              |
| `dsh`      | `~/.dsh`                                               |
| `hermes`   | `~/.hermes`                                            |

Codex, opencode, Auggie, Tau, jcode, dsh and Hermes also read skills from the
shared `~/.agents/skills` directory, so their import copies it too (category
`skills`). Freebuff reads that directory as well, but outside its persisted
config mount, so it is not imported for Freebuff.

Use `--home PATH` when your agent config lives under a different home.

## Profiles

By default the import writes into the active [profile](profiles.md). To send it
somewhere else, or to copy between profiles:

```bash
vp import claude --to-profile work --create-profile
vp import claude --from-profile default --to-profile work
```

Safety rules, in order:

1. The destination profile must exist. A missing one is an error naming
   `vp profile create <name>` — or pass `--create-profile`.
2. If the destination agent directory already holds any of the files the import
   would write, the run **aborts and writes nothing**, listing the conflicts.
3. `--force` overwrites exactly those files and leaves everything else in place.
   It never wipes the directory.

Symlinks inside the source are never followed and never copied; each one is
reported. The one exception is the agent directory itself (or a parent such as
`~/.config`): when a dotfile manager such as stow or chezmoi has made it a
symlink, it is resolved once and imported from its target, with the same rule
applied to everything inside it. Nothing is written through a symlink in the
destination: the agent directory is mounted read-write into its container, so
such a file is reported and left alone.

If everything found was skipped (only credentials without `--with-credentials`,
only symlinks, only unrecognized files without `--with-other`), the run says
why and exits non-zero instead of reporting an import of zero files; a
`--dry-run` only warns.

Each file is written to a temporary file and renamed into place, so a single
destination file is never left half-written. Execute bits are kept, so hook
and skill helper scripts still run inside the pod. The import as a whole is not
atomic, though: if one file fails (unreadable source, full disk) the others are
still copied, the failures are listed and the command exits non-zero. Fix the
cause and re-run with `--force`.

## Credentials

Credentials are left behind unless you pass `--with-credentials`, so an import
does not silently duplicate a token into another sandbox. Besides each agent's
known token files, any file found inside a copied directory whose name looks
like a secret (`*oauth*`, `*credential*`, `*token*`, `*secret*`, `auth.json`,
`*-auth.json`, `*_auth.json`, `.env`, `*.key`, `*.pem`) is treated as a credential too. Copied credential
files are written `0600` inside a `0700` directory.

!!! note "Claude Code on macOS"
    Claude Code stores its OAuth token in the macOS Keychain, not in
    `~/.claude/.credentials.json`. There is usually nothing to copy — run
    `vp run claude` and log in inside the pod instead.

opencode (`~/.local/share/opencode/auth.json`), Codex (`~/.codex/auth.json`),
Gemini (`~/.gemini/oauth_creds.json`, `mcp-oauth-tokens.json`, `.env`), Auggie
(`~/.augment/session.json`), Copilot (`~/.copilot/config.json`, which holds the
token when no system keychain is available), Pi (`~/.pi/agent/auth.json`), Tau
(`~/.tau/credentials.json`), jcode (`~/.jcode/auth.json`, `openai-auth.json` and the provider env
files under `~/.config/jcode`), Freebuff
(`~/.config/manicode/credentials.json`), Qwen (`~/.qwen/oauth_creds.json`,
`.env`), dsh (`~/.dsh/.credentials.yaml`) and Hermes (`~/.hermes/.env`,
`~/.hermes/auth.json`) do keep credentials on disk, so `--with-credentials`
works for them.

## Model providers

The `models` category copies each agent's own provider files (Pi's
`models.json`, Tau's `providers.json` and `catalog.toml`, …) as they are. To
define an endpoint once and route any supported agent through it, use the
[global model provider registry](providers.md) instead.
[`vp provider import`](providers.md#sharing-providers) reads a shared provider
definition file and is unrelated to `vp import`.

## Known limits

- **Host paths are reported, not rewritten.** A hook command or MCP server
  entry pointing at `/Users/you/bin/something` (or `C:\Users\you\bin\...` on
  Windows) will not resolve in the container, where the project is mounted at
  `/workspace`. The import warns per file and leaves the value untouched.
- **`~/.claude.json` is not mounted by VibePod.** It holds Claude Code's
  user-level MCP servers and project history; the container writes its own copy
  that does not persist. Define MCP servers in a project-level `.mcp.json`
  instead.
