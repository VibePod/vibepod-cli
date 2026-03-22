#!/usr/bin/env bash
set -euo pipefail

RUNNER_TEMP="${RUNNER_TEMP:-/tmp}"
PODMAN_PID_FILE="${RUNNER_TEMP}/podman-service.pid"

if [[ -f "${PODMAN_PID_FILE}" ]]; then
  kill "$(cat "${PODMAN_PID_FILE}")" >/dev/null 2>&1 || true
  rm -f "${PODMAN_PID_FILE}"
fi
