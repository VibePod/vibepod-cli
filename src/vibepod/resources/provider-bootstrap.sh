#!/bin/sh
# Container-local temporary provider configuration for the jcode image, which
# ships neither Node nor Python: POSIX sh plus coreutils (mktemp, ln, base64).
#
# Mirrors provider-bootstrap.cjs/.py. The real agent argv arrives as
# VIBEPOD_PROVIDER_COMMAND_B64 (one base64 word per argument, each prefixed with
# "b" so empty arguments survive word splitting); the provider sections are
# rendered by the CLI into VIBEPOD_PROVIDER_CONFIG_TOML (keys referenced by
# environment variable name only). This script is launched as `sh <mounted
# path>` alone, so image entrypoints that re-parse argv cannot mangle prompts.
#
# jcode honours JCODE_HOME, which replaces ~/.jcode and maps
# $JCODE_HOME/config/jcode and $JCODE_HOME/external/<path> to the real
# ~/.config/jcode and ~/<path>. HOME itself stays untouched, sessions stay
# persistent, and the native config.toml is never written back.

fail() {
  printf '%s\n' "$1" >&2
  exit 1
}

[ -n "${HOME:-}" ] || fail "Cannot prepare temporary provider configuration."
[ -n "${VIBEPOD_PROVIDER_COMMAND_B64:-}" ] || fail "Cannot prepare temporary provider configuration."
[ -n "${VIBEPOD_PROVIDER_CONFIG_TOML:-}" ] || fail "Cannot prepare temporary provider configuration."
[ -n "${VIBEPOD_PROVIDER_NAMES:-}" ] || fail "Cannot prepare temporary provider configuration."

# Rebuild "$@" from the encoded words; trailing newlines are kept via the
# sentinel because command substitution strips them.
set --
for part in $VIBEPOD_PROVIDER_COMMAND_B64; do
  case "$part" in b*) ;; *) fail "Cannot prepare temporary provider configuration." ;; esac
  decoded=$(printf '%s' "${part#b}" | base64 -d && printf x) \
    || fail "Cannot prepare temporary provider configuration."
  set -- "$@" "${decoded%x}"
done
[ "$#" -gt 0 ] || fail "Cannot prepare temporary provider configuration."

real="$HOME/.jcode"
app_config="$HOME/.config/jcode"
# Persistent jcode directories must exist before the view links to them.
mkdir -p "$real/sessions" "$real/logs" "$real/state" "$app_config" \
  || fail "Cannot prepare temporary provider configuration."
chmod 700 "$real" "$app_config" 2>/dev/null || true

if [ -f "$real/config.toml" ]; then
  for name in $VIBEPOD_PROVIDER_NAMES; do
    if grep -q "^\[providers\.$name\]" "$real/config.toml"; then
      fail "Provider name already defined in the native jcode configuration: $name. Rename the VibePod provider."
    fi
  done
fi

view=$(mktemp -d "${TMPDIR:-/tmp}/vibepod-provider-XXXXXX") \
  || fail "Cannot prepare temporary provider configuration."
chmod 700 "$view"
cleanup() { rm -rf "$view"; }

# Symlink every entry of the real ~/.jcode except the private config.
for entry in "$real"/* "$real"/.[!.]* "$real"/..?*; do
  [ -e "$entry" ] || [ -L "$entry" ] || continue
  name=${entry##*/}
  case "$name" in config.toml|config|external) continue ;; esac
  ln -s "$entry" "$view/$name" || { cleanup; fail "Cannot prepare temporary provider configuration."; }
done
mkdir "$view/config" && chmod 700 "$view/config" \
  && ln -s "$app_config" "$view/config/jcode" \
  && ln -s "$HOME" "$view/external" \
  || { cleanup; fail "Cannot prepare temporary provider configuration."; }

umask 077
{
  if [ -f "$real/config.toml" ]; then
    cat "$real/config.toml"
    # Ensure the appended sections start on their own line.
    [ -z "$(tail -c 1 "$real/config.toml")" ] || printf '\n'
    printf '\n'
  fi
  printf '%s\n' "$VIBEPOD_PROVIDER_CONFIG_TOML"
} > "$view/config.toml" || { cleanup; fail "Cannot prepare temporary provider configuration."; }

unset VIBEPOD_PROVIDER_COMMAND_B64 VIBEPOD_PROVIDER_CONFIG_TOML VIBEPOD_PROVIDER_NAMES VIBEPOD_PROVIDER_PLAN
JCODE_HOME="$view"
export JCODE_HOME

# Run the agent as a child so the view can be removed afterwards. The
# original stdin is duplicated explicitly: background jobs otherwise read
# /dev/null. SIGINT reaches the child through the terminal already; TERM and
# HUP (docker stop) are forwarded.
exec 3<&0
"$@" <&3 &
pid=$!
exec 3<&-
trap 'kill -TERM "$pid" 2>/dev/null' TERM HUP
wait "$pid"
status=$?
while [ "$status" -gt 128 ] && kill -0 "$pid" 2>/dev/null; do
  wait "$pid"
  status=$?
done
cleanup
exit "$status"
