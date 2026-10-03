# Development

This page covers local setup and common commands for contributing to VibePod.

Backend investigations are recorded in architecture decisions. See
[VP-153: Contemper agent VMs](decisions/vp-153-contemper.md) for the no-go
decision and requirements for reconsidering that image authoring route.

## Prerequisites

- Python 3.10+
- Docker (running)

## Install development dependencies

Clone the repository, then install in editable mode with development and docs extras:

```bash
pip install -e ".[dev,docs]"
```

## Run tests and checks

```bash
pytest
ruff check .
mypy src
```

## Work on documentation

Serve docs locally with live reload:

```bash
mkdocs serve
```

Build the static docs site:

```bash
mkdocs build
```
