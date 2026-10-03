#!/bin/sh
# Managed by VibePod — reports Codex lifecycle hook events to VibePod Dash.
# Receives one JSON object on stdin and always exits 0 so a dashboard outage
# never disturbs the agent.
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
value = data
for part in sys.argv[1].split("."):
    if not isinstance(value, dict):
        value = None
        break
    value = value.get(part)
if isinstance(value, (dict, list)):
    value = json.dumps(value)
print("" if value is None else str(value).replace("\n", " ")[:300])
' "$1" 2>/dev/null
    }
else
    field() {
        key=${1##*.}
        printf '%s' "$payload" | tr '\n' ' ' \
            | sed -n "s/.*\"$key\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" \
            | cut -c1-300
    }
fi

event=$(field hook_event_name)
session_id=$(field session_id)
cwd=$(field cwd)
[ -n "$cwd" ] || cwd="$PWD"

case "$event" in
    SessionStart)
        state=idle
        message="session started"
        ;;
    UserPromptSubmit)
        state=working
        message=$(field prompt)
        ;;
    PreToolUse|PostToolUse)
        state=working
        message=$(field tool_name)
        ;;
    PermissionRequest)
        state=blocked
        message=$(field tool_input.description)
        [ -n "$message" ] || message="$(field tool_name) needs approval"
        ;;
    Stop)
        state=idle
        message=$(field last_assistant_message)
        [ -n "$message" ] || message="waiting for you"
        ;;
    Interrupt)
        state=idle
        message="turn interrupted"
        ;;
    SessionEnd)
        state=done
        message="session ended"
        ;;
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
    VPDASH_AGENT="${VPDASH_AGENT:-codex}" "$reporter" \
        --state "$state" \
        --event "$event" \
        --message "$message" \
        --session "$session_id" \
        --cwd "$cwd" 2>&1
)
rc=$?
log "report state=$state event=$event rc=$rc${out:+ out=$out}"

exit 0
