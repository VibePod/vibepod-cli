#!/usr/bin/env bash
set -euo pipefail

SCENARIO="${1:?missing runtime scenario}"
RUNNER_TEMP="${RUNNER_TEMP:-/tmp}"
XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-${RUNNER_TEMP}/xdg-runtime}"
PODMAN_DIR="${XDG_RUNTIME_DIR}/podman"
PODMAN_SOCKET_PATH="${PODMAN_DIR}/podman.sock"
PODMAN_SOCKET_URL="unix://${PODMAN_SOCKET_PATH}"
PODMAN_PID_FILE="${RUNNER_TEMP}/podman-service.pid"
PODMAN_LOG_FILE="${RUNNER_TEMP}/podman-service.log"

wait_for_docker() {
  for _ in $(seq 1 30); do
    if docker version >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  docker version
}

wait_for_podman() {
  for _ in $(seq 1 30); do
    if [[ -S "${PODMAN_SOCKET_PATH}" ]] && podman --url "${PODMAN_SOCKET_URL}" version >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  podman --url "${PODMAN_SOCKET_URL}" version
}

ensure_docker_started() {
  if command -v systemctl >/dev/null 2>&1; then
    sudo systemctl start docker.socket || true
    sudo systemctl start docker.service || true
  fi
  if command -v service >/dev/null 2>&1; then
    sudo service docker start || true
  fi
  wait_for_docker
}

stop_docker() {
  if command -v systemctl >/dev/null 2>&1; then
    sudo systemctl stop docker.service || true
    sudo systemctl stop docker.socket || true
  fi
  if command -v service >/dev/null 2>&1; then
    sudo service docker stop || true
  fi
  sleep 2
}

install_podman() {
  sudo touch /etc/subuid /etc/subgid

  if ! grep -q "^${USER}:" /etc/subuid; then
    echo "${USER}:100000:65536" | sudo tee -a /etc/subuid >/dev/null
  fi
  if ! grep -q "^${USER}:" /etc/subgid; then
    echo "${USER}:100000:65536" | sudo tee -a /etc/subgid >/dev/null
  fi

  if command -v podman >/dev/null 2>&1; then
    return 0
  fi

  sudo apt-get update
  sudo apt-get install -y podman
}

stop_podman_service() {
  if [[ -f "${PODMAN_PID_FILE}" ]]; then
    kill "$(cat "${PODMAN_PID_FILE}")" >/dev/null 2>&1 || true
    rm -f "${PODMAN_PID_FILE}"
  fi
  rm -f "${PODMAN_SOCKET_PATH}"
}

start_podman_service() {
  install_podman
  stop_podman_service

  mkdir -p "${PODMAN_DIR}"
  chmod 700 "${XDG_RUNTIME_DIR}"

  nohup podman system service --time=0 "${PODMAN_SOCKET_URL}" >"${PODMAN_LOG_FILE}" 2>&1 &
  echo "$!" >"${PODMAN_PID_FILE}"

  wait_for_podman
}

case "${SCENARIO}" in
  docker-only)
    stop_podman_service
    ensure_docker_started
    ;;
  podman-only)
    start_podman_service
    stop_docker
    ;;
  none)
    stop_podman_service
    stop_docker
    ;;
  both-auto|both-default-docker|both-default-podman|both-switch)
    ensure_docker_started
    start_podman_service
    ;;
  *)
    echo "Unknown runtime scenario: ${SCENARIO}" >&2
    exit 1
    ;;
esac

echo "Prepared runtime scenario: ${SCENARIO}"
docker version >/dev/null 2>&1 && docker version --format '{{.Server.Version}}' || true
command -v podman >/dev/null 2>&1 && podman --url "${PODMAN_SOCKET_URL}" version --format '{{.Server.Version}}' || true
