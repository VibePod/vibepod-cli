# Tutorials

The rest of these docs are **reference pages**: each one explains a single
feature (agents, profiles, providers, overlays, configuration) in full.
Tutorials work the other way round. Each one picks a concrete goal and walks
through it end to end in short steps, combining several features along the
way. When a step needs more detail, it links to the reference page instead of
repeating it.

## Before you start

You need:

- Docker **or** Podman, running
- VibePod installed (`pip install vibepod`, or another method from the
  [Quickstart](../quickstart.md#install))
- A project directory to work in, ideally a Git repository

If you have never started an agent with VibePod, run through the
[Quickstart](../quickstart.md) first. The tutorials assume you can already run
`vp run claude` (or another agent) and log in.

## Available tutorials

- [**Compare two agents on the same task**](compare-agents.md): give Claude
  and Codex the same prompt in separate copies of a project, then compare
  their results and their traffic in the dashboard.
- [**Share a team setup**](team-setup.md): commit a project config and an
  overlay so everyone on the team gets the same agent defaults and tools,
  while credentials stay on each machine.
- [**Use your own model provider**](own-model-provider.md): point an agent at
  a local or hosted model server, and pick the right mechanism for each agent.
