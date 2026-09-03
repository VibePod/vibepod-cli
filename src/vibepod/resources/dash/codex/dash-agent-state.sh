#!/bin/sh
# Codex notify program → VibePod Dash. Codex passes the event as a JSON string
# argument (not on stdin). Register it in ~/.codex/config.toml:
#
#   notify = ["/path/to/dash-agent-state.sh"]
#
# Needs VPDASH_URL (and VPDASH_TOKEN) in the environment. Set VPDASH_LOG to a
# writable path to trace what the hook did. Always exits 0.
set -u

log() {
    [ -n "${VPDASH_LOG:-}" ] || return 0
    printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || echo -)" "$1" \
        >>"$VPDASH_LOG" 2>/dev/null || true
}

url="${VPDASH_URL:-}"
if [ -z "$url" ]; then
    log "skip reason=VPDASH_URL unset"
    exit 0
fi
payload="${1:-}"

field() {
    printf '%s' "$payload" | tr '\n' ' ' \
        | sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" \
        | cut -c1-300
}

kind=$(field type)
case "$kind" in
    agent-turn-complete) state=idle; message=$(field last-assistant-message) ;;
    *) state=working; message="$kind" ;;
esac

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
reporter="$script_dir/vpdash-report.sh"
if [ ! -x "$reporter" ]; then
    log "skip type=${kind:-?} reason=reporter missing at $reporter"
    exit 0
fi

out=$(
    VPDASH_AGENT="${VPDASH_AGENT:-codex}" "$reporter" \
        --state "$state" \
        --event "${kind:-notify}" \
        --message "${message:-${kind:-notify}}" \
        --session "$(field turn-id)" 2>&1
)
rc=$?
log "report state=$state type=${kind:-?} rc=$rc${out:+ out=$out}"

exit 0
