#!/bin/sh
# Managed by VibePod — reports Copilot CLI hook events to herdr.
# Event vocabulary tolerates naming drift. Transport: node + socket API
# (primary), herdr binary (fallback). Traced to
# $HOME/.copilot/herdr-hook.log (host-visible via the config mount).
set -u

log_file="${HOME:-/tmp}/.copilot/herdr-hook.log"
log() {
    printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || echo -)" "$1" \
        >>"$log_file" 2>/dev/null || true
}

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
reporter="$script_dir/herdr-report.js"

payload=$(cat 2>/dev/null || true)
event=$(printf '%s' "$payload" \
    | tr -d '\n' \
    | sed -n 's/.*"\(hook_event_name\|type\)"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\2/p')

if [ -z "${HERDR_PANE_ID:-}" ]; then
    log "skip event=${event:-?} reason=HERDR_PANE_ID unset"
    exit 0
fi

send_state() {
    if command -v node >/dev/null 2>&1 && [ -f "$reporter" ] \
        && [ -n "${HERDR_SOCKET_PATH:-}" ]; then
        out=$(node "$reporter" pane.report_agent copilot "$1" 2>&1)
        rc=$?
        via=socket
    elif [ -n "${HERDR_BIN_PATH:-}" ] && [ -x "$HERDR_BIN_PATH" ]; then
        out=$("$HERDR_BIN_PATH" pane report-agent "$HERDR_PANE_ID" \
            --source vibepod --agent copilot --state "$1" 2>&1)
        rc=$?
        via=binary
    else
        log "skip event=${event:-?} reason=no transport (node+reporter or herdr binary)"
        return
    fi
    log "report state=$1 event=$event via=$via rc=$rc${out:+ out=$out}"
}

# blocked patterns first: approval events often contain "tool" too
# (e.g. ToolApprovalRequest) and must not be classified as working
case "$event" in
    *[Nn]otif*|*[Pp]ermission*|*[Aa]pproval*) send_state blocked ;;
    *[Pp]rompt*|*[Tt]ool*) send_state working ;;
    *[Ss]top*|*[Ee]nd*|*[Cc]omplete*) send_state idle ;;
    *) log "ignore event=${event:-?}" ;;
esac

exit 0
