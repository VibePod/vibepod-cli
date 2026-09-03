# Codex Lifecycle Hooks Design

## Objective

Replace the legacy Codex `notify` integration used by VibePod's dash and
herdr integrations with Codex lifecycle hooks. Both integrations must report
accurate state transitions, coexist in the same Codex configuration, preserve
user configuration, and continue to soft-fail. This change does not modify or
rebuild any agent image.

## Background

Codex `notify` currently emits only `agent-turn-complete`. Consequently, the
existing dash and herdr adapters can report only `idle`; they cannot observe a
turn starting, tool activity, an approval request, interruption, or session
shutdown. Codex also permits only one `notify` command, so the integration
configured first prevents the other one from registering.

Current Codex releases expose these events through `hooks.json`. Multiple
handlers can be registered for an event, which allows dash and herdr to remain
independent while receiving the same lifecycle event.

## Configuration architecture

A shared `vibepod.core.codex_hooks` helper will merge one integration's command
into `<config-dir>/.codex/hooks.json`. The helper accepts a command path and
the events that should invoke it. For every event it appends a command handler
only when that exact command is not already present.

The merge must preserve:

- existing top-level fields, including `description`;
- existing hook events and matcher groups;
- user-provided handlers in events also used by VibePod;
- handlers installed independently by dash and herdr; and
- a user-defined top-level `notify` setting in `config.toml`.

If `hooks.json` is missing, the helper creates it with a top-level `hooks`
object. If the file is malformed, its top level is not an object, or its
`hooks` value is not an object, VibePod warns and leaves it unchanged. A broken
hook configuration must never block an agent run.

VibePod will remove a legacy `notify` assignment only when its parsed value
exactly matches one of VibePod's former dash or herdr commands. This migration
applies whether one or both integrations are currently enabled, so a stale
VibePod assignment cannot continue claiming the user's single notification
slot. Other `notify` values are untouched.

## Event adapters

Codex lifecycle hooks pass one JSON object on standard input. The dash and
herdr Codex adapters will consume that input and inspect `hook_event_name`.
They remain POSIX shell scripts and emit no standard output, because several
Codex hook events treat command output as control data.

Events map to state as follows:

| Codex event | State | Message or metadata |
| --- | --- | --- |
| `SessionStart` | `idle` | Session started; herdr records session identity |
| `UserPromptSubmit` | `working` | Prompt where supported |
| `PreToolUse` | `working` | Tool name |
| `PostToolUse` | `working` | Tool name |
| `PermissionRequest` | `blocked` | Tool name or approval description |
| `Stop` | `idle` | Latest assistant message or waiting state |
| `Interrupt` | `idle` | Turn interrupted |
| `SessionEnd` | `done` for dash; `idle` for herdr | Session ended |

Herdr has no terminal `done` state, so `SessionEnd` remains `idle` until the
existing container cleanup releases the herdr agent entry. Unknown events are
logged and ignored.

Handlers run synchronously to preserve event ordering. Each adapter already
contains bounded network/socket operations and catches failures, so dashboard
or herdr outages remain non-fatal.

## Integration behavior

Dash and herdr each register their own command handler for every supported
event. Registration order is irrelevant: both commands remain in
`hooks.json`, and disabling one integration does not disable or rewrite the
other one's handlers.

The existing injected reporter implementations and container environment
contract remain unchanged. Only the Codex lifecycle adapter and registration
mechanism change. The dash HTTP reporter remains the verbatim vendored client.
To preserve the repository rule that `resources/dash` stays synchronized with
the upstream dash repository, the lifecycle adapter used by VibePod will live
under `resources/codex` and call the vendored HTTP reporter. The old vendored
Codex `notify` adapter remains untouched but is no longer injected.

Codex requires non-managed hooks to be reviewed and trusted. Documentation and
doctor output will identify `hooks.json` registration so users can use Codex's
`/hooks` interface when a new definition awaits trust.

## Diagnostics and documentation

`vp doctor herdr` and `vp doctor dash` will detect the appropriate command in
`.codex/hooks.json` rather than looking for it in `config.toml`. Diagnostic
checks must distinguish a missing file, malformed JSON, and an absent handler
without raising.

The dash and herdr documentation will describe lifecycle-hook support and the
one-time Codex trust step. References claiming that Codex lacks working or
blocked events, or that integrations compete for one `notify` program, will be
removed.

## Testing

Tests will cover:

- creation and idempotent merging of Codex `hooks.json`;
- preservation of existing user hooks and unrelated top-level fields;
- coexistence of dash and herdr handlers regardless of registration order;
- preservation of user-defined `notify` commands;
- removal of only the two legacy VibePod `notify` commands;
- soft failure for malformed `hooks.json` and `config.toml`;
- stdin parsing and state mapping for every supported Codex event;
- session metadata reporting to herdr;
- doctor recognition of lifecycle-hook registration; and
- unchanged behavior for non-Codex agents.

The focused dash, herdr, doctor, run, and task tests will run with an isolated
`VP_CONFIG_DIR`, followed by the full unit suite, Ruff formatting/linting, and
mypy.

## Non-goals

- Changing the `vibepod/codex` image or selecting a different image tag.
- Supporting Codex versions that expose `notify` but not lifecycle hooks.
- Combining dash and herdr into one dispatcher or making either integration
  depend on the other.
- Changing lifecycle behavior for Claude, Copilot, or other agents.
