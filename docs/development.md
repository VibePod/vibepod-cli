# Development

This page covers local setup and common commands for contributing to VibePod.

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
