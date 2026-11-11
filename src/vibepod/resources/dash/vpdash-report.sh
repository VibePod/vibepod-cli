#!/bin/sh
# Report one agent event to a VibePod Dash server.
#
#   vpdash-report.sh --state working --event PreToolUse --message "Edit app.py"
#
# Environment:
#   VPDASH_URL         base URL of the dash server (required), e.g. http://dash.local:8765
#   VPDASH_TOKEN       ingest token (required unless the server runs without one)
#   VPDASH_AGENT       agent kind, default "agent"
#   VPDASH_AGENT_ID    stable id; when unset the server derives one
#   VPDASH_AGENT_NAME  display name; when unset the server derives one
#   VPDASH_HOST        host label, default `hostname`
#
# Exits 0 even when the server is unreachable: a dashboard must never take an
# agent down with it.
set -u

url="${VPDASH_URL:-}"
[ -n "$url" ] || exit 0

agent="${VPDASH_AGENT:-agent}"
agent_id="${VPDASH_AGENT_ID:-}"
name="${VPDASH_AGENT_NAME:-}"
host="${VPDASH_HOST:-$(hostname 2>/dev/null || echo unknown)}"
cwd="$PWD"
session=""
state=""
event=""
message=""

while [ $# -gt 0 ]; do
    case "$1" in
        --state) state="${2:-}"; shift 2 ;;
        --event) event="${2:-}"; shift 2 ;;
        --message) message="${2:-}"; shift 2 ;;
        --agent) agent="${2:-}"; shift 2 ;;
        --id) agent_id="${2:-}"; shift 2 ;;
        --name) name="${2:-}"; shift 2 ;;
        --session) session="${2:-}"; shift 2 ;;
        --cwd) cwd="${2:-}"; shift 2 ;;
        --host) host="${2:-}"; shift 2 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "vpdash-report: unknown argument $1" >&2; exit 2 ;;
    esac
done

# JSON string body: escape backslashes and quotes, drop control characters.
esc() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr -d '\000-\037'
}

# Emit `"key":"value",` only for values that are set.
pair() {
    [ -n "$2" ] || return 0
    printf '"%s":"%s",' "$1" "$(esc "$2")"
}

payload=$(
    printf '{'
    pair agent "$agent"
    pair agent_id "$agent_id"
    pair name "$name"
    pair state "$state"
    pair event "$event"
    pair message "$message"
    pair session_id "$session"
    pair host "$host"
    printf '"cwd":"%s"}' "$(esc "$cwd")"
)

# -S keeps curl's error message even in silent mode, and http_code exposes a
# rejection (401 without a token, say) that curl itself would call success.
# Callers (hooks) route stderr into their trace log.
out=$(
    curl -sS -o /dev/null -w 'http_code=%{http_code}' --connect-timeout 1 --max-time 3 \
        -X POST "${url%/}/api/v1/events" \
        -H "Content-Type: application/json" \
        ${VPDASH_TOKEN:+-H "Authorization: Bearer $VPDASH_TOKEN"} \
        -d "$payload" 2>&1
) || true

case "$out" in
    *http_code=2*) ;;  # accepted
    *http_code=401* | *http_code=403*)
        printf 'vpdash-report: %s (is VPDASH_TOKEN the dash ingest token?)\n' "$out" >&2
        ;;
    *) printf 'vpdash-report: %s\n' "$out" >&2 ;;
esac

exit 0
