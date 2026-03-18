# Using OSS Models via Ollama, vLLM, and other LLM servers

VibePod can connect agents to external LLM servers that expose OpenAI- or Anthropic-compatible APIs. This lets you run agents like Claude Code and Codex against open-source models served by [Ollama](https://ollama.com), [vLLM](https://docs.vllm.ai), or any compatible endpoint.

## Supported agents

| Agent | Env vars injected | CLI flags appended |
|-------|------------------|--------------------|
| claude | `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_OPUS_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_HAIKU_MODEL` | `--model <model>` |
| codex | `CODEX_OSS_BASE_URL` | `--oss -m <model>` |

Other agents do not yet have LLM mapping and will not receive any LLM configuration.

## Quick start with Ollama

### 1. Start Ollama and pull a model

```bash
ollama pull qwen3:14b
```

### 2. Configure VibePod

Add the following to your global or project config:

```yaml
# ~/.config/vibepod/config.yaml
llm:
  enabled: true
  base_url: "http://host.docker.internal:11434"
  api_key: "ollama"
  model: "qwen3:14b"
```

!!! note
    Use `host.docker.internal` (not `localhost`) so the Docker container can reach Ollama on the host machine.

### 3. Run an agent

```bash
vp run claude
# Starts Claude Code with:
#   ANTHROPIC_BASE_URL=http://host.docker.internal:11434
#   ANTHROPIC_API_KEY=ollama
#   ANTHROPIC_AUTH_TOKEN=ollama
#   ANTHROPIC_MODEL=qwen3:14b
#   ANTHROPIC_DEFAULT_OPUS_MODEL=qwen3:14b
#   ANTHROPIC_DEFAULT_SONNET_MODEL=qwen3:14b
#   ANTHROPIC_DEFAULT_HAIKU_MODEL=qwen3:14b
#   claude --model qwen3:14b

vp run codex
# Starts Codex with:
#   CODEX_OSS_BASE_URL=http://host.docker.internal:11434
#   codex --oss -m qwen3:14b
```

## Using environment variables

You can also configure LLM settings at runtime without editing config files.

**Claude Code with a remote Ollama server:**

```bash
VP_LLM_ENABLED=true VP_LLM_MODEL=qwen3.5:9b VP_LLM_BASE_URL=https://ollama.example.com vp run claude
```

**Codex with a remote Ollama server (note the `/v1` suffix):**

```bash
VP_LLM_ENABLED=true VP_LLM_MODEL=qwen3.5:9b VP_LLM_BASE_URL=https://ollama.example.com/v1 vp run codex
```

**Local Ollama with an API key:**

```bash
VP_LLM_ENABLED=true VP_LLM_BASE_URL=http://host.docker.internal:11434 VP_LLM_API_KEY=ollama VP_LLM_MODEL=qwen3:14b vp run claude
```

!!! note
    Claude Code uses the Anthropic-compatible endpoint (no `/v1` suffix), while Codex uses the OpenAI-compatible endpoint (with `/v1` suffix). Adjust `VP_LLM_BASE_URL` accordingly, or use per-agent overrides if you need both agents to work from the same config.

See [Configuration > Environment variables](configuration.md#environment-variables) for the full list.

## Using vLLM or other OpenAI-compatible servers

Point `base_url` at any server that speaks the OpenAI or Anthropic API:

```yaml
llm:
  enabled: true
  base_url: "http://my-vllm-server:8000/v1"
  api_key: "my-api-key"
  model: "meta-llama/Llama-3-8B-Instruct"
```

## Per-agent overrides

If you need different LLM settings for a specific agent, use the per-agent `env` config. Per-agent env vars take precedence over the `llm` section:

```yaml
llm:
  enabled: true
  base_url: "http://host.docker.internal:11434"
  api_key: "ollama"
  model: "qwen3:14b"

agents:
  claude:
    env:
      ANTHROPIC_BASE_URL: "http://different-server:11434"
```

## Disabling

To turn off LLM injection without removing the config:

```yaml
llm:
  enabled: false
```

Or at runtime:

```bash
VP_LLM_ENABLED=false vp run claude
```
