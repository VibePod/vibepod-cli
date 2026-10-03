# Show you use VibePod

Add a "built with VibePod" badge to your project's README or docs:

[![Built with VibePod](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/VibePod/vibepod-cli/main/.github/badges/vibepod.json)](https://vibepod.dev)

The badge is a [shields.io endpoint badge](https://shields.io/badges/endpoint-badge). Its label,
color, and inline logo come from
[`.github/badges/vibepod.json`](https://github.com/VibePod/vibepod-cli/blob/main/.github/badges/vibepod.json)
in this repository, so it picks up logo updates automatically.

## Snippets

=== "Markdown"

    ```markdown
    [![Built with VibePod](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/VibePod/vibepod-cli/main/.github/badges/vibepod.json)](https://vibepod.dev)
    ```

=== "HTML"

    ```html
    <a href="https://vibepod.dev"><img alt="Built with VibePod" src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/VibePod/vibepod-cli/main/.github/badges/vibepod.json" /></a>
    ```

=== "reStructuredText"

    ```rst
    .. image:: https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/VibePod/vibepod-cli/main/.github/badges/vibepod.json
       :target: https://vibepod.dev
       :alt: Built with VibePod
    ```

## Styles

Append a shields.io `style` parameter to the image URL to match your other badges:

| Style | Parameter |
| ----- | --------- |
| `flat` (default) | — |
| `flat-square` | `&style=flat-square` |
| `plastic` | `&style=plastic` |
| `for-the-badge` | `&style=for-the-badge` |
| `social` | `&style=social` |

For example:

```markdown
[![Built with VibePod](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/VibePod/vibepod-cli/main/.github/badges/vibepod.json&style=for-the-badge)](https://vibepod.dev)
```
