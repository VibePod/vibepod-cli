#!/bin/sh
# Copilot CLI hook → VibePod Dash. Receives the hook payload as JSON on stdin.
# The event vocabulary tolerates naming drift: matching is on substrings, and
# approval-ish events win over tool-ish ones (ToolApprovalRequest contains
# both). Needs VPDASH_URL (and VPDASH_TOKEN); set VPDASH_LOG to trace.
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

payload=$(cat 2>/dev/null || true)
event=$(printf '%s' "$payload" | tr '\n' ' ' \
    | sed -n 's/.*"\(hook_event_name\|type\)"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\2/p')

case "$event" in
    *[Nn]otif* | *[Pp]ermission* | *[Aa]pproval*) state=blocked ;;
    *[Pp]rompt* | *[Tt]ool*) state=working ;;
    *[Ss]top* | *[Ee]nd* | *[Cc]omplete*) state=idle ;;
    *)
        log "ignore event=${event:-?}"
        exit 0
        ;;
esac

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
reporter="$script_dir/vpdash-report.sh"
if [ ! -x "$reporter" ]; then
    log "skip event=$event reason=reporter missing at $reporter"
    exit 0
fi

out=$(
    VPDASH_AGENT="${VPDASH_AGENT:-copilot}" "$reporter" \
        --state "$state" --event "$event" --message "$event" 2>&1
)
rc=$?
log "report state=$state event=$event rc=$rc${out:+ out=$out}"

exit 0
