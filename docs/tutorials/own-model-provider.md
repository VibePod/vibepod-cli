# Use your own model provider

In this tutorial you point agents at a model server of your choice: a local
[Ollama](https://ollama.com) server in the examples, but any compatible
hosted or self-hosted endpoint works the same way. You will register the server
once as a VibePod [model provider](../providers.md) and select it per launch
with `--provider`. You will also see when an agent-scoped `env` block or the
older `llm:` config section fits better.

## Pick the mechanism first

VibePod has three ways to route an agent to another endpoint. They differ in
which agents they support and in what they affect:

| Mechanism | Affects | Agents |
| --------- | ------- | ------ |
| [`vp provider`](../providers.md) + `--provider NAME` | the one launch that passes the flag | Claude, Codex, Pi, Qwen, Tau, Jcode, OpenCode |
| [`agents.<agent>.env`](../agents/index.md#passing-environment-variables) | every launch of that one agent | any agent whose CLI reads its endpoint from environment variables, such as Claude (`ANTHROPIC_*`) or Qwen (`OPENAI_*`) |
| [`llm:`](../llm.md) | every launch of **every** agent with legacy LLM mapping | Claude and Codex only |

Each agent also needs a matching API protocol:

| Agent | Protocols accepted with `--provider` |
| ----- | ------------------------------------ |
| Claude | Anthropic Messages |
| Codex | OpenAI Responses |
| Qwen | OpenAI Chat Completions, Anthropic Messages |
| Pi, Tau, OpenCode | OpenAI Chat Completions, OpenAI Responses, Anthropic Messages |
| Jcode | OpenAI Chat Completions |

Gemini, Copilot, Auggie, Agy, Freebuff, Devstral, and dsh have no verified
custom-endpoint mechanism in VibePod, and Hermes is configured with
`hermes setup` inside its container. See
[Temporary launch routing](../providers.md#temporary-launch-routing) for the
full rules.

## 1. Make the server reachable from containers

Agents run in containers, where `localhost` is the container itself. A
provider has one URL that is used both for model discovery on your host and for
requests from inside the agent container, so it needs an address that works
from both sides. The simplest choice is your machine's LAN address: bind the
server to it and use that IP in the provider URL.

For Ollama on Linux, that means `OLLAMA_HOST=0.0.0.0` (or the LAN IP itself);
read the security note in [LLM integration](../llm.md#1-start-ollama-and-pull-a-model)
before you widen the binding. The examples below use `192.168.1.10` as the
server address; replace it with yours.

[Local providers](../providers.md#local-providers) describes two other
patterns: the `host.docker.internal` alias and a model server container on the
VibePod network.

## 2. Register the provider

Run the wizard:

```bash
vp provider add
```

An abbreviated session for an Ollama server, using its OpenAI-compatible API
(your model list will differ):

```text
Provider name: ollama-lan
API protocol (openai-chat, openai-responses, anthropic) [openai-chat]:
API base URL: http://192.168.1.10:11434/v1
Authentication (none, key, env) [none]:
Discover models now? [Y/n]:
...
Use all discovered models? [Y/n]: y
Default model (empty keeps native selection) []: qwen3:14b
...
Saved provider 'ollama-lan'. Native agent configuration is unchanged.
```

Set a default model: Claude, Codex, and Qwen need one to launch with a
provider. Check the result:

```bash
vp provider list
vp provider models ollama-lan
```

The definition is stored under `~/.vibepod/providers/ollama-lan/`, outside
any project, and is available to every profile.

!!! tip "Hosted endpoints"
    For a hosted API, choose `env` authentication and give the name of an
    environment variable that holds the key; VibePod reads it from your shell
    at launch time. Authenticated launches require an `https://` URL. Ready-made
    definitions for common hosted and local servers can be imported with
    `vp provider import`; see [Provider templates](../providers.md#provider-templates).

## 3. Launch an agent with the provider

Pass `--provider` to `vp run`:

```bash
vp run pi --provider ollama-lan
```

Pi starts on the provider's default model, and every selected model is
available in Pi's `/model` picker. The same flag works for Qwen, Tau, Jcode,
and OpenCode with this Chat Completions provider:

```bash
vp run qwen --provider ollama-lan
```

The routing applies to this launch only. VibePod passes it to the agent through
environment variables or a temporary copy of the agent's configuration, and
never writes the provider into the agent's own settings, so the next
`vp run qwen` without the flag uses Qwen's native setup again. While the agent runs, `vp list` shows the provider name in its
`PROVIDER` column.

Task mode accepts the flag too, for every agent in the table above except Pi
and OpenCode:

```bash
vp task create qwen "Summarize the README" --provider ollama-lan
```

## 4. Use the provider with Claude or Codex

Claude only speaks the Anthropic Messages protocol and Codex only the OpenAI
Responses API, so they reject the `openai-chat` provider from step 2.

Recent Ollama versions also serve the Anthropic Messages API. Register a second
provider with protocol `anthropic` and the base URL **without** `/v1`
(Anthropic clients append it themselves):

```text
Provider name: ollama-lan-anthropic
API protocol (openai-chat, openai-responses, anthropic) [openai-chat]: anthropic
API base URL: http://192.168.1.10:11434
...
```

Then launch Claude with it:

```bash
vp run claude --provider ollama-lan-anthropic
```

!!! note "Conflicting environment settings"
    For Claude, `--provider` sets `ANTHROPIC_BASE_URL`, the API key and model
    variables itself. If `agents.claude.env` or a `-e` flag already sets one
    of those variables (or `CLAUDE_CODE_OAUTH_TOKEN`), the launch stops with a
    "Provider selection conflicts with environment settings" error. Remove the
    variable, or use the env approach from step 5 instead of `--provider`.

For Codex, register a provider with protocol `openai-responses`, and only if
the server actually supports the Responses API. Then run
`vp run codex --provider NAME`.

## 5. Make it the default for one agent

`--provider` is a per-launch flag; there is no config key that selects a
provider automatically. To route one agent to your server on every launch,
set that agent's own environment variables in `agents.<agent>.env`. They
apply to that agent only.

For Claude:

```yaml
# ~/.config/vibepod/config.yaml or .vibepod/config.yaml
agents:
  claude:
    env:
      ANTHROPIC_BASE_URL: http://192.168.1.10:11434
      ANTHROPIC_API_KEY: ollama # placeholder; Ollama does not check it
      ANTHROPIC_AUTH_TOKEN: ollama
      ANTHROPIC_MODEL: qwen3:14b
```

For Qwen, which reads the standard OpenAI variables:

```yaml
agents:
  qwen:
    env:
      OPENAI_BASE_URL: http://192.168.1.10:11434/v1
      OPENAI_API_KEY: ollama
      OPENAI_MODEL: qwen3:14b
```

Put real API keys only in your personal global config, never in a committed
project file (see [Share a team setup](team-setup.md#4-keep-credentials-and-personal-settings-out-of-the-repository)).
Remember the conflict rule above: with these variables set, `--provider` is
rejected for that agent.

## When to use `llm:` instead

The [`llm:`](../llm.md) section predates providers. It has **no per-agent
scope**: when `llm.enabled` is true, VibePod injects the settings into every
launch of every agent that has a legacy LLM mapping, which today means Claude
and Codex:

- Claude gets `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`,
  `ANTHROPIC_AUTH_TOKEN`, the `ANTHROPIC_*MODEL` variables, and
  `--model <model>`.
- Codex gets `CODEX_OSS_BASE_URL` and `--oss -m <model>`.

Claude needs the Anthropic-style URL (no `/v1`) and Codex the OpenAI-style URL
(with `/v1`), so one `base_url` rarely suits both. While `llm.enabled` is true,
VibePod also refuses to start Hermes. Variables set in `agents.<agent>.env`
take precedence over the injected values, and a launch with `--provider` skips
`llm:` injection entirely.

If you only want to try the `llm:` route once, use the environment variables
instead of editing a config file:

```bash
VP_LLM_ENABLED=true VP_LLM_BASE_URL=http://192.168.1.10:11434 \
  VP_LLM_API_KEY=ollama VP_LLM_MODEL=qwen3:14b vp run claude
```

For anything you want to keep, prefer a provider (per launch) or
`agents.<agent>.env` (per agent).

## Clean up

Remove a provider, including any stored key, with:

```bash
vp provider remove ollama-lan
```

## Next steps

- [Model providers](../providers.md): model settings (context window, output
  limit, reasoning levels), sharing provider files, and how each agent's
  temporary configuration works.
- [LLM integration](../llm.md): exposing Ollama safely to containers.
- [Compare two agents on the same task](compare-agents.md): run the same prompt
  against different models and compare the results.
