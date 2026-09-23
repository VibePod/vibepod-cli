# Global model providers (in development)

Register a compatible hosted endpoint or local model server without editing files:

```sh
vp provider add
vp provider list
vp provider edit my-provider
vp provider models my-provider
vp provider models my-provider --refresh
vp provider refresh my-provider
vp provider remove my-provider
```

The wizard asks for the API protocol, base URL, authentication, selected model IDs, and an optional default model. It supports
OpenAI Chat Completions, OpenAI Responses, and Anthropic Messages as distinct
protocols. Model discovery uses the endpoint's compatible model-list API; enter
model IDs manually when that endpoint is unavailable. Listing a model does not
prove tool calling, streaming, vision, or protocol compatibility.

After successful discovery, **Use all discovered models?** defaults to Yes.
Decline to enter specific IDs. This selects the current catalog; it does not
silently enable models added by the server in the future.

Use `vp provider edit NAME` to change prefilled settings, keep or replace the
stored key, and refresh the model catalog. After refreshing, accept all discovered
models to include newly installed local models or additions to a hosted catalog.
The current default is preserved if it remains selected. Enter `-` to clear the
default model. Cancelling before saving leaves settings
and credentials unchanged. Changes apply to future launches, not running agents.

`vp provider refresh NAME` re-discovers models, asks which ones to use and which
is the default, and changes nothing else; settings of models that stay selected
are kept. `vp provider models NAME --refresh` updates only the discovery cache,
not your selected models or default. A failed refresh keeps the existing cache.

## Model settings

Discovery only yields model IDs. Context window, output limit, and reasoning
controls are never guessed. `vp provider add` and `vp provider edit` offer to
set them after the model selection: choose the models to configure (Enter picks
the default model), then answer context window, max output tokens, reasoning
model, accepted reasoning levels (Enter keeps all), and default level. `edit`
prefills saved values and forgets settings of models that are no longer
selected. `vp provider models NAME` lists the model IDs with their settings.

Reasoning levels are the portable set `off, minimal, low, medium, high, xhigh`;
each adapter maps them to the agent's own names and hides the rest. The default
level applies at launch to the provider's default model only.

What each agent receives (only fields you set; unset fields stay agent defaults):

| Agent | Context window | Max output | Reasoning | Levels / default |
| --- | --- | --- | --- | --- |
| Pi | `contextWindow` | `maxTokens` | `reasoning` | `thinkingLevelMap` (unsupported levels `null`, `xhigh` mapped for OpenAI-style APIs); default via `--thinking` |
| Codex | `model_context_window` | not a Codex setting | via effort | `model_reasoning_effort` from the default level (`off` → `none`) |
| OpenCode | `limit.context` (with output) | `limit.output` (with context) | `reasoning` | no portable control |
| Tau | `model_metadata.<id>.context_window` | `max_tokens` | `reasoning` | `unsupported_thinking_levels`, provider `thinking_parameter` by protocol, `thinking_default` |
| Jcode | `context_window` per model | no setting | no setting | no setting |
| Claude | no setting | `CLAUDE_CODE_MAX_OUTPUT_TOKENS` | native | native |
| Qwen | no setting | no setting | no setting | no setting |

OpenCode's `limit` needs both values; with only one set, nothing is emitted.

## Credentials and storage

Definitions are global across profiles, under `~/.vibepod/providers/<name>/`:

- `provider.toml`: URL, protocol, authentication reference, selected models.
- `credentials.json` (or `credentials-<id>.json` after rotation): API key, only
  when stored-key authentication is selected. Metadata references the active file;
  rotation publishes a new reference atomically before removing the old key.
- `models.json`: cached discovered model IDs and refresh time, when available.

`VP_PROVIDERS_DIR` overrides the provider-store directory. This is deliberately
separate from the existing `~/.config/vibepod/` configuration/profile root.

Stored keys are **plaintext**, protected by owner-only directory/file permissions.
Processes running as your user and backups can still read them. The wizard masks
key input and asks before storing a key. Alternatively, reference an environment
variable or select no authentication for a local endpoint. Current storage
requires POSIX owner/permission support; it fails closed on unsupported platforms.
Do not put the store in a project or version-control repository.

Discovery shows its destination before sending requests. TLS certificates are
verified; redirects are refused. Sending a key over HTTP for discovery requires
explicit confirmation. A stored-key provider is never activated automatically.

## Temporary launch routing

| Agent | Protocols | Selection |
| --- | --- | --- |
| Claude | Anthropic Messages | One provider, saved default model required |
| Pi | OpenAI Chat Completions, Responses, Anthropic Messages | One or more providers; models available in `/model` |
| Codex | OpenAI Responses | One provider, saved default model required |
| Qwen | OpenAI Chat Completions, Anthropic Messages | One provider, saved default model required; routed via `OPENAI_*`/`ANTHROPIC_*` env |
| Tau | OpenAI Chat Completions, Responses, Anthropic Messages | One or more providers; private `catalog.toml` view; a single provider with a saved default is selected via Tau's `--provider`/`--model` |
| Jcode | OpenAI Chat Completions only (jcode named profiles); plain `http` only to `localhost`, `*.local`, or private IPs (jcode's own rule, so register the LAN IP, not `host.docker.internal`) | One or more providers; private `config.toml` profiles under `JCODE_HOME`; a saved default is selected via `--provider-profile`/`--model` |
| OpenCode | OpenAI Chat Completions, Responses, Anthropic Messages | One or more providers; private `opencode.json` view |

```sh
vp run pi --provider llmapi
vp run pi --profile work --provider local --provider hosted
vp run codex --provider responses-endpoint
vp run claude --provider anthropic-endpoint
```

For Pi, a single provider's saved default selects the startup model. With multiple
providers, or no saved default, native model selection is retained; the injected
models are available in `/model`. No provider is arbitrarily picked as default.
Pi's own defaults apply to unspecified model metadata; discovery does not establish
context limits or vision/tool capabilities.

Codex requires the Responses API. An endpoint advertising OpenAI compatibility
but supporting only Chat Completions cannot be used with this adapter. Use
`vp provider edit NAME` to select `openai-responses` only if the server actually
supports it. The adapter uses Codex's native custom-provider configuration,
not its `--oss` mode. The registry stores all selected model IDs, but Codex starts
with the provider's saved default; it does not get Pi's custom model-picker catalog.

An explicit `--model`/`-m` passed through to the agent (after `--`) takes
precedence over the provider's saved default; the provider routing itself is
still injected.

Selected providers replace legacy `llm` injection for that launch; conflicting
explicit routing environment settings cause an error. Without `--provider`,
existing native/legacy behavior is unchanged. Provider selection does not mount
the global store or alter the original profile's provider configuration.

### Configuration lifetime

Claude uses environment variables. Pi and Codex use a private configuration
view created **inside the container** by its Node runtime after user mapping.
Pi's `models.json`, settings, and authentication are copied/merged privately;
Codex's config/auth files are private copies with native command-line overrides.
Unrelated native Pi providers remain available. Saved credentials for an injected
Pi provider cannot override its selected launch key.

OpenCode merges its configuration sources in a fixed order and reads
`OPENCODE_CONFIG_DIR` last, so the private view becomes that directory: the
profile's `opencode.json` (VibePod points `OPENCODE_CONFIG_DIR` at the mounted
config root) is copied with the injected `provider` entries and, for a single
provider with a saved default, `model = "<name>/<model>"`; every other entry of
that directory is symlinked through and `XDG_CONFIG_HOME` is untouched. A
profile that keeps an `opencode.jsonc` in that directory cannot be overridden
safely and fails the launch with a message; convert it to `opencode.json`.

`vp list` marks each running container with the injected provider names in its
`PROVIDER` column (comma-separated when several `--provider` flags were used).

Agents may still rewrite their own files on startup as they always do (opencode
inserts `$schema` into its config, Qwen Code stamps `$version` into its settings);
that is native behaviour, not provider injection.

Session directories stay linked to the active profile, so conversations persist.
Settings changed in the temporary view are not written back. Login state follows
each agent's own store: Pi, Codex, and Tau get private copies of their
credential files for the launch (a login there is discarded), while Jcode's
`auth.json` and OpenCode's XDG data directory stay the profile's own, so logins
and token refreshes in those sessions persist as usual. Existing profile
credentials are never removed: provider selection is not a credential isolation
sandbox. Concurrent launches get separate configuration directories.

Temporary files are removed when the agent process exits normally. Background
containers own their files independently of the CLI process. A forced kill can
leave private files inside a retained container until it is removed; no temporary
provider files are left on the host. Restarting a retained container rebuilds the
view using that container's original launch environment.

Environment credentials remain visible to container-runtime administrators and
in retained containers until removal. Authenticated launches currently require
an HTTPS endpoint. Unauthenticated local endpoints can use HTTP; Pi and Claude
receive a placeholder key because their clients require one.

The bootstrap script itself is copied once to `<config root>/runtime/`
(`~/.config/vibepod/runtime/provider-bootstrap.cjs`, honoring `VP_CONFIG_DIR`)
and bind-mounted read-only at `/opt/vibepod/provider-bootstrap.cjs` (likewise
`.py` for Tau and `.sh` for Jcode). It contains no secrets. The container command is only `node` plus that path; the real agent
command travels in `VIBEPOD_PROVIDER_COMMAND` (JSON), which the bootstrap unsets
before starting the agent. This keeps the launch working on older Codex images
whose entrypoint re-parses argv through `sh -c "$*"`. That entrypoint still
mangles ordinary passthrough arguments containing quotes or spaces, which the
`vibepod-agents` Codex entrypoint fix addresses independently.

**Bootstrap runtimes:** Pi, Codex, and OpenCode use the image's Node; Tau uses
its Python; Jcode, whose image ships neither, uses POSIX `sh` with coreutils
(`mktemp`, `ln`, `base64`). For Jcode the `[providers.<name>]` sections are
rendered by the CLI and passed in `VIBEPOD_PROVIDER_CONFIG_TOML` (keys by
environment variable name only), and the agent argv travels base64-encoded per
argument in `VIBEPOD_PROVIDER_COMMAND_B64`. No image change is required.
Tau has no config-root override, so inside its view `HOME` points at the
private directory; every other entry of the real home (`.gitconfig`, `.ssh`,
`.config`, caches) is symlinked through, so git identity and other tools keep
working. The private `catalog.toml` is the profile's own catalog plus appended
`[[providers]]` entries (Tau schema version 1); `providers.json` and
`credentials.json` are private, unmodified copies. Jcode keeps `HOME` and gets
`JCODE_HOME` pointing at a view whose `config.toml` is the profile's file plus
appended `[providers.<name>]` profiles; `config/jcode` and `external/` link back
to the real `~/.config/jcode` and home. A VibePod provider whose name already
exists in the native Tau catalog or Jcode config fails the launch explicitly;
rename the VibePod provider.

**Remaining limitations:** provider injection in ACP mode is not implemented
for the agents that need a private configuration view (Pi, Codex, Tau, Jcode,
OpenCode) and is rejected there; the environment-routed agents (Claude, Qwen)
accept `--provider` in ACP mode. Unsupported `vp run` selections fail explicitly. OpenCode has no headless mode, so it
cannot take `--provider` in task mode; the same applies to Pi. Hermes is deliberately out of scope for
this feature: its runtime prioritizes saved provider settings, ignores
`OPENAI_BASE_URL`, and restricts keys by endpoint host, so use `hermes setup`
inside the container (see the [Hermes notes](agents/index.md#hermes-agent-hermes-developer-preview)).
Gemini, Copilot, Auggie, Agy, Freebuff, Devstral, and dsh have no verified
custom-endpoint mechanism and are rejected as well.

Qwen, Tau, Jcode, and OpenCode follow the same temporary-launch rules as the
first three agents: saved credentials and native configuration are never
written back, provider selection replaces legacy `llm` injection for that
launch, and conflicting routing environment settings cause an error. Injected
Tau/Jcode profiles reference the launch key by environment variable only, so
saved native credentials for other providers are untouched. Task mode accepts
`--provider` for every agent in the table above except OpenCode and Pi, which
have no headless mode in VibePod.

### Docker smoke tests

`.github/workflows/provider-smoke.yml` builds a real image per supported agent and
runs it against a local fake streaming API, checking routing, preserved profile files,
and persistent sessions. No real API keys or paid services are used. The companion
`vibepod-agents` ref must include the Codex entrypoint fix: select it with the manual
workflow's `agents_ref` input or the `PROVIDER_AGENTS_REF` repository variable while
the change is under development; the default is `main`. The provider wrapper itself
no longer depends on that fix; the smoke test's prompt argument does.

The job runs `tests/integration/test_provider_smoke.py` with `VP_PROVIDER_SMOKE=1`
and `VP_PROVIDER_SMOKE_AGENT` set to one of `pi`, `codex`, `qwen`, `tau`, `jcode`,
`opencode`. Ordinary unit tests do not need Docker.

## Local providers

Ollama, Lemonade Server, and other servers can use generic discovery when they
expose a compatible `/models` endpoint. Enter the API base URL the server actually
provides, for example `http://localhost:11434/v1` for an OpenAI-compatible Ollama
endpoint. For the Anthropic Messages protocol, enter the URL **without** a `/v1`
suffix (for example `http://localhost:11434`): Anthropic clients append `/v1`
themselves, and the wizard rejects URLs that already end with it. Native
discovery APIs and branded server presets are not implemented.

A provider has one URL, used for discovery on the host and for inference inside
the agent container. `localhost` inside the container is the container itself,
so a server on your machine needs an address that works from both sides. Three
patterns:

**LAN address (recommended).** Bind the server to your machine's LAN IP (for
Ollama: `OLLAMA_HOST=0.0.0.0` or the IP itself) and register
`http://192.168.1.10:11434/v1`. Discovery and inference both work, and the same
provider serves a server on another machine unchanged.

**Host gateway alias.** VibePod adds `host.docker.internal` to every agent
container (Docker Desktop resolves it natively; on Linux Docker it maps to the
host gateway). Register `http://host.docker.internal:11434/v1` and bind the
server to `0.0.0.0` or the Docker bridge address; a server bound to `127.0.0.1`
only is not reachable this way. The host cannot resolve that name, so decline
discovery and enter the model IDs manually.

**Model server as a container on the VibePod network.** Attach it to the
network agents use (`network` in the VibePod config, default `vibepod-network`)
and register its container name:

```sh
docker run -d --name ollama --network vibepod-network \
  -v ollama:/root/.ollama ollama/ollama
vp provider add   # API base URL: http://ollama:11434/v1
```

Nothing needs to be published to the host, and the server is unreachable from
outside the network. Discovery from the host cannot resolve the container name,
so enter model IDs manually (or publish the port on `127.0.0.1` temporarily for
discovery and switch the URL back with `vp provider edit`).

Prefer restricted bindings and firewall rules over exposing an unauthenticated
server on all interfaces. Provider setup does not change server bindings, open
ports, or bypass VibePod proxy allow/deny policies. See
[LLM integration](llm.md) for further networking guidance.

### Example: Ollama on another machine

An Ollama server on a Mac mini in the LAN, reached by IP, which works from the
host and from every agent container:

```text
$ vp provider add
Provider name: local-m4
API protocol (openai-chat, openai-responses, anthropic) [openai-chat]:
API base URL: http://192.168.178.85:11434/v1
Authentication (none, key, env) [none]:
Discover models now? [Y/n]:
Discovery destination: http://192.168.178.85:11434/v1
Contact this endpoint to list models? [Y/n]:
devstral-small-2:24b
gemma4:12b-mlx
glm-4.7-flash:latest
granite4.1:3b
granite4.1:8b
granite4:3b
lfm2.5-thinking:1.2b
lfm2.5-thinking:latest
lfm2:24b
ministral-3:14b
nemotron-cascade-2:30b
ornith-1.5:9b
qwen3-embedding:8b
qwen3.5:27b
qwen3.5:9b
qwen3.8:27b-mlx
translategemma:12b
Use all discovered models? [Y/n]: y
Default model (empty keeps native selection) []: gemma4:12b-mlx
Saved provider 'local-m4'. Native agent configuration is unchanged.
```

Launch an agent with it for one session; the profile's own provider setup is
untouched:

```sh
vp run pi --provider local-m4    # starts on gemma4:12b-mlx, others in /model
vp run tau --provider local-m4   # tau --provider local-m4 --model gemma4:12b-mlx
```

The provider uses the Chat Completions protocol, so Claude (Anthropic only) and
Codex (Responses only) reject it. Ollama also serves the Anthropic Messages API
on recent versions: register a second provider with protocol `anthropic` and
base URL `http://192.168.178.85:11434` (no `/v1`) to use it with Claude.
