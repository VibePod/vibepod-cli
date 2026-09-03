# Vendored VibePod Dash clients

These files are copied **verbatim** from `clients/` in
[VibePod/vibepod-dash](https://github.com/VibePod/vibepod-dash) and injected
into an agent's config directory by `vibepod.core.dash`.

Do not edit them here in isolation: change them in vibepod-dash first (its
end-to-end tests run them against a real server), then copy the result back.
They may only assume POSIX `sh` and `curl` — agent images often have neither
`node`, `jq` nor `python3` — and must exit 0 on every failure path.
