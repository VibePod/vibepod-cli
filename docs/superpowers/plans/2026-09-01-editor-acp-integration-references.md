# Editor ACP Integration References Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add concise, accurate VibePod ACP configuration references for Zed, PyCharm/JetBrains IDEs, and Visual Studio Code.

**Architecture:** Keep shared allow-list and authentication setup in `docs/acp.md`, then add one compact subsection per editor. Use the same absolute `vp` command and explicit workspace arguments in every example, while clearly distinguishing native ACP clients from the third-party VS Code extension.

**Tech Stack:** Material for MkDocs, Markdown, JSON configuration examples, `mkdocs build --strict`

---

## File structure

- Modify `docs/acp.md`: shared ACP setup plus the three editor integration references.
- No production code, test code, navigation, or configuration files change.

### Task 1: Add the editor integration references

**Files:**
- Modify: `docs/acp.md:37-81`

- [ ] **Step 1: Replace the Zed-only setup text with shared setup guidance**

Keep the existing `## Setup` heading. Explain that editors launch `vp` as a
subprocess, recommend an absolute executable path, and retain these commands:

````markdown
Register `vp` as an external or custom ACP agent in your editor. Use an
absolute path to the executable because GUI applications do not necessarily
inherit your shell `PATH`. Pass `-w` explicitly when the editor's subprocess
working directory is not guaranteed to be the open project.

Allow the project directory before opening the first editor session — editor
stdin is a protocol pipe, so the interactive allow prompt cannot run:

```bash
vp config allow-dir /absolute/path/to/project
```

Authenticate the selected agent once interactively when its ACP adapter or
editor does not provide the required login flow:

```bash
vp run <agent>   # sign in, then quit
```

Credentials persist in the agent's config directory. Replace `claude` in the
examples below with any agent from the supported-agent table.
````

- [ ] **Step 2: Add the Zed native integration reference**

Add this subsection and configuration immediately after the shared setup:

````markdown
## Editor integrations

### Zed

[Zed supports ACP External Agents natively](https://zed.dev/docs/ai/external-agents).
Open Agent Settings, select **External Agents**, then **Add Agent** →
**Add Custom Agent**, or add this entry to `settings.json`:

```json
{
  "agent_servers": {
    "VibePod Claude": {
      "type": "custom",
      "command": "/absolute/path/to/vp",
      "args": [
        "run",
        "claude",
        "--acp",
        "-w",
        "/absolute/path/to/project"
      ],
      "env": {}
    }
  }
}
```

Select **VibePod Claude** when starting an External Agent thread. Run
`dev: open acp logs` from the command palette to inspect startup errors and
protocol traffic.
````

- [ ] **Step 3: Add the native PyCharm and JetBrains integration reference**

Append this subsection:

````markdown
### PyCharm and other JetBrains IDEs

JetBrains IDEs with a current AI Assistant plugin
[support custom ACP agents natively](https://www.jetbrains.com/help/ai-assistant/acp.html).
In AI Chat, open the **More** menu and select **Add Custom Agent**. This creates
`~/.jetbrains/acp.json`; add:

```json
{
  "default_mcp_settings": {},
  "agent_servers": {
    "VibePod Claude": {
      "command": "/absolute/path/to/vp",
      "args": [
        "run",
        "claude",
        "--acp",
        "-w",
        "/absolute/path/to/project"
      ],
      "env": {}
    }
  }
}
```

Select **VibePod Claude** in AI Chat. Use **Get ACP Logs** from the AI Chat
**More** menu for diagnostics.

!!! warning "JetBrains IDEs do not currently support ACP agents through WSL"

    Use PyCharm on native Linux or macOS for this integration. This is a
    JetBrains client limitation; the Zed remote-WSL setup documented below is
    unaffected.
````

- [ ] **Step 4: Add the third-party Visual Studio Code integration reference**

Append this subsection, explicitly avoiding any native-support claim:

````markdown
### Visual Studio Code

Visual Studio Code requires an ACP client extension. The example below uses the
third-party
[ACP Client extension](https://marketplace.visualstudio.com/items?itemName=formulahendry.acp-client).
Install it, run **ACP: Add Agent Configuration** from the command palette, or
add this to your user or workspace `settings.json`:

```json
{
  "acp.agents": {
    "VibePod Claude": {
      "command": "/absolute/path/to/vp",
      "args": [
        "run",
        "claude",
        "--acp",
        "-w",
        "/absolute/path/to/project"
      ],
      "env": {}
    }
  },
  "acp.autoApprovePermissions": "ask",
  "acp.logTraffic": true
}
```

Open the ACP Client panel and connect to **VibePod Claude**. Use
**ACP: Show Log** or **ACP: Show Protocol Traffic** for diagnostics.
````

- [ ] **Step 5: Remove duplicated setup and debugging text**

Delete the old Zed-only example, the “Repeat the block” paragraph, and the
blanket statement that ACP sessions cannot authenticate. Keep the final
instruction to open the editor's agent UI only if it adds information not
already present in the three editor subsections.

Update `## Debugging` to avoid being Zed-only:

```markdown
VibePod diagnostics, including a missing `vp config allow-dir`, go to stderr
and appear in the editor's ACP logs. Use the log command named in the relevant
editor integration above.
```

- [ ] **Step 6: Inspect the documentation diff**

Run:

```bash
git diff -- docs/acp.md
```

Expected: only the Setup, Editor integrations, and Debugging portions change;
the supported-agent, transport, Windows/WSL, and limitation sections remain
otherwise intact.

### Task 2: Validate and commit the documentation

**Files:**
- Test: `docs/acp.md`

- [ ] **Step 1: Validate every JSON example**

Copy each fenced JSON object from the three editor subsections into `python -m
json.tool`, or use an equivalent JSON parser. Expected: all three objects parse
without errors.

- [ ] **Step 2: Build the documentation strictly**

Run:

```bash
python -m mkdocs build --strict
```

Expected: exit status 0 with no warnings. If the active environment lacks
MkDocs, report that limitation and rely on the exact-head documentation CI
rather than claiming a local build passed.

- [ ] **Step 3: Run whitespace validation**

Run:

```bash
git diff --check
```

Expected: exit status 0 and no output.

- [ ] **Step 4: Confirm scope and source accuracy**

Verify that:

- Zed and JetBrains are described as native ACP clients.
- VS Code is described as using the third-party `formulahendry.acp-client`
  extension.
- All commands use `vp run claude --acp -w /absolute/path/to/project`.
- Only the JetBrains subsection makes the current no-WSL claim.
- Zed, JetBrains, and Marketplace links point to their authoritative pages.

- [ ] **Step 5: Commit the integration references**

```bash
git add docs/acp.md
git commit -m "docs: add editor ACP integration references"
```

Expected: one documentation commit containing only `docs/acp.md`.
