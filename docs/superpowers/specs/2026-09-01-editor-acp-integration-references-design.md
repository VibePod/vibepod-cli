# Editor ACP integration references

## Goal

Add concise, copyable integration references for Zed, PyCharm/JetBrains IDEs,
and Visual Studio Code to the existing ACP documentation. Keep shared VibePod
setup in one place and avoid turning the page into separate editor tutorials.

## Placement and structure

Replace the Zed-only example in `docs/acp.md` with an **Editor integrations**
section under **Setup**. Begin with the shared requirements already established
by the page: allow the project directory, authenticate the selected agent when
needed, use an absolute path to `vp`, and pass the workspace explicitly with
`-w` when the editor's subprocess working directory is uncertain.

The section will contain one compact subsection per editor:

- **Zed** — identify support as native, show an `agent_servers` custom-agent
  entry, link to Zed's External Agents documentation, and name
  `dev: open acp logs` for diagnostics.
- **PyCharm and other JetBrains IDEs** — identify support as native through AI
  Assistant, show a `~/.jetbrains/acp.json` entry, link to JetBrains' ACP
  documentation, name **Get ACP Logs**, and state JetBrains' current WSL
  limitation.
- **Visual Studio Code** — state clearly that the reference uses the
  third-party **ACP Client** extension rather than native VS Code support, show
  an `acp.agents` entry in `settings.json`, link to its Marketplace page, and
  name the ACP Client and ACP Traffic output channels for diagnostics.

All examples use the same display name and launch shape:

```text
/absolute/path/to/vp run claude --acp -w /absolute/path/to/project
```

Readers can replace `claude` with any agent in the supported-agent table.

## Platform guidance

Preserve the existing general Windows/WSL section. Do not imply that every
editor supports the same remote-development path: explicitly call out the
JetBrains WSL limitation, retain Zed's documented remote-WSL route, and make no
new VS Code WSL claim without an authoritative source.

## Validation

- Build the documentation with `mkdocs build --strict`.
- Run `git diff --check`.
- Review every JSON example for valid syntax and verify that command arguments
  match `vp run <agent> --acp -w <workspace>`.
- Confirm links target the current official Zed and JetBrains documentation and
  the exact VS Code Marketplace extension being referenced.

## Out of scope

- Installing VibePod from an editable checkout.
- Full per-editor authentication walkthroughs.
- ACP Registry submission or automatic editor configuration.
- Claims that VS Code has native ACP support.
