#!/bin/sh
# Managed by VibePod — reports Claude Code hook events to herdr.
# Receives the hook payload as JSON on stdin; always exits 0 so a broken
# herdr setup never disturbs the agent. Transport: node + socket API
# (primary), herdr binary (fallback). Traced to
# $CLAUDE_CONFIG_DIR/herdr-hook.log (host-visible via the config mount).
set -u

log_file="${CLAUDE_CONFIG_DIR:-${HOME:-/tmp}}/herdr-hook.log"
log() {
    printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || echo -)" "$1" \
        >>"$log_file" 2>/dev/null || true
}

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
reporter="$script_dir/herdr-report.js"

payload=$(cat 2>/dev/null || true)

json_field() {
    printf '%s' "$payload" \
        | tr -d '\n' \
        | sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p"
}

event=$(json_field "hook_event_name")
session_id=$(json_field "session_id")
transcript=$(json_field "transcript_path")

if [ -z "${HERDR_PANE_ID:-}" ]; then
    log "skip event=${event:-?} reason=HERDR_PANE_ID unset"
    exit 0
fi

# send <method> <state-or-empty> <session-id-or-empty> <session-path-or-empty>
send() {
    if command -v node >/dev/null 2>&1 && [ -f "$reporter" ] \
        && [ -n "${HERDR_SOCKET_PATH:-}" ]; then
        out=$(node "$reporter" "$1" claude "$2" "$3" "$4" 2>&1)
        rc=$?
        via=socket
    elif [ -n "${HERDR_BIN_PATH:-}" ] && [ -x "$HERDR_BIN_PATH" ]; then
        if [ "$1" = "pane.report_agent" ]; then
            out=$("$HERDR_BIN_PATH" pane report-agent "$HERDR_PANE_ID" \
                --source vibepod --agent claude --state "$2" \
                ${3:+--agent-session-id "$3"} 2>&1)
        else
            out=$("$HERDR_BIN_PATH" pane report-agent-session "$HERDR_PANE_ID" \
                --source vibepod --agent claude \
                ${3:+--agent-session-id "$3"} \
                ${4:+--agent-session-path "$4"} 2>&1)
        fi
        rc=$?
        via=binary
    else
        log "skip event=${event:-?} reason=no transport (node+reporter or herdr binary)"
        return
    fi
    log "$1 state=${2:-–} event=$event via=$via rc=$rc${out:+ out=$out}"
}

case "$event" in
    SessionStart)
        send pane.report_agent_session "" "$session_id" "$transcript"
        send pane.report_agent idle "$session_id" ""
        ;;
    UserPromptSubmit|PreToolUse|PostToolUse)
        send pane.report_agent working "$session_id" ""
        ;;
    Notification)
        send pane.report_agent blocked "$session_id" ""
        ;;
    Stop|SessionEnd)
        send pane.report_agent idle "$session_id" ""
        ;;
    *)
        log "ignore event=${event:-?}"
        ;;
esac

exit 0
