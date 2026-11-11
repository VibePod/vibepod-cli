#!/bin/sh
# Claude Code hook → VibePod Dash. Receives the hook payload as JSON on stdin
# and maps the lifecycle event to a dashboard state. Always exits 0 so a dash
# outage never disturbs the agent.
#
# Install with clients/install-claude-hooks.py, or register it manually for
# SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Notification, Stop
# and SessionEnd. Needs VPDASH_URL (and VPDASH_TOKEN) in the environment.
#
# Set VPDASH_LOG to a writable path to trace what the hook did (VibePod points
# it at the mounted agent config dir, so the log is readable from the host).
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

# python3 when the image has it (handles nested objects and escapes), a
# newline-flattened sed otherwise.
if command -v python3 >/dev/null 2>&1; then
    field() {
        printf '%s' "$payload" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
value = data.get(sys.argv[1])
if isinstance(value, (dict, list)):
    value = json.dumps(value)
print("" if value is None else str(value).replace("\n", " ")[:300])
' "$1" 2>/dev/null
    }
else
    field() {
        printf '%s' "$payload" | tr '\n' ' ' \
            | sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" \
            | cut -c1-300
    }
fi

event=$(field hook_event_name)
session=$(field session_id)
cwd=$(field cwd)
[ -n "$cwd" ] || cwd="$PWD"

case "$event" in
    SessionStart)    state=idle;    message="session started" ;;
    UserPromptSubmit) state=working; message=$(field prompt) ;;
    PreToolUse)      state=working; message=$(field tool_name) ;;
    PostToolUse)     state=working; message=$(field tool_name) ;;
    Notification)    state=blocked; message=$(field message) ;;
    Stop)            state=idle;    message="waiting for you" ;;
    SessionEnd)      state=done;    message="session ended" ;;
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
    VPDASH_AGENT="${VPDASH_AGENT:-claude}" "$reporter" \
        --state "$state" \
        --event "$event" \
        --message "${message:-$event}" \
        --session "$session" \
        --cwd "$cwd" 2>&1
)
rc=$?
log "report state=$state event=$event rc=$rc${out:+ out=$out}"

exit 0
