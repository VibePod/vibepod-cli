#!/bin/sh
# Managed by VibePod — reports Codex notify events to herdr.
# Codex passes a single JSON argument, e.g. {"type":"agent-turn-complete",...}.
# Transport: node + socket API (primary), herdr binary (fallback). Traced to
# $HOME/.codex/herdr-hook.log (host-visible via the config mount).
set -u

log_file="${HOME:-/tmp}/.codex/herdr-hook.log"
log() {
    printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || echo -)" "$1" \
        >>"$log_file" 2>/dev/null || true
}

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
reporter="$script_dir/herdr-report.js"

payload="${1:-}"
type=$(printf '%s' "$payload" \
    | tr -d '\n' \
    | sed -n 's/.*"type"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')

if [ -z "${HERDR_PANE_ID:-}" ]; then
    log "skip type=${type:-?} reason=HERDR_PANE_ID unset"
    exit 0
fi

send_state() {
    if command -v node >/dev/null 2>&1 && [ -f "$reporter" ] \
        && [ -n "${HERDR_SOCKET_PATH:-}" ]; then
        out=$(node "$reporter" pane.report_agent codex "$1" 2>&1)
        rc=$?
        via=socket
    elif [ -n "${HERDR_BIN_PATH:-}" ] && [ -x "$HERDR_BIN_PATH" ]; then
        out=$("$HERDR_BIN_PATH" pane report-agent "$HERDR_PANE_ID" \
            --source vibepod --agent codex --state "$1" 2>&1)
        rc=$?
        via=binary
    else
        log "skip type=${type:-?} reason=no transport (node+reporter or herdr binary)"
        return
    fi
    log "report state=$1 type=$type via=$via rc=$rc${out:+ out=$out}"
}

case "$type" in
    agent-turn-complete)
        send_state idle
        ;;
    *)
        log "ignore type=${type:-?}"
        ;;
esac

exit 0
