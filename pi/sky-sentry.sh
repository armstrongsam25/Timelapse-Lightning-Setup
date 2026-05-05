#!/usr/bin/env bash
# Flash the Arduino sky_sentry sketch and start the Pi timelapse.
#
# Used both for ad-hoc launches and as the ExecStart of the systemd service
# (see sky-sentry.service). On every start it re-flashes the Arduino so the
# sensor firmware can never drift out of sync with what's in the repo, then
# execs into timelapse.py so systemd tracks the camera process directly.
#
# Override anything via environment variables (the systemd unit sets these):
#   REPO_DIR          path to the repo on disk          default: this script's parent's parent
#   SKETCH_DIR        Arduino sketch to flash           default: $REPO_DIR/arduino/sky_sentry
#   ARDUINO_PORT      serial port for the Arduino       default: /dev/ttyACM0
#   ARDUINO_FQBN      arduino-cli FQBN                  default: arduino:avr:uno
#   ARDUINO_CLI       path to arduino-cli               default: arduino-cli (on PATH)
#   SKIP_FLASH        set to 1 to skip the flash step   default: unset
#   TIMELAPSE_ARGS    extra args passed to timelapse.py default: empty (uses script defaults)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
SKETCH_DIR="${SKETCH_DIR:-$REPO_DIR/arduino/sky_sentry}"
ARDUINO_PORT="${ARDUINO_PORT:-/dev/ttyACM0}"
ARDUINO_FQBN="${ARDUINO_FQBN:-arduino:avr:uno}"
ARDUINO_CLI="${ARDUINO_CLI:-arduino-cli}"
TIMELAPSE_ARGS="${TIMELAPSE_ARGS:-}"

log() { printf '[sky-sentry] %s\n' "$*"; }

if [[ "${SKIP_FLASH:-0}" == "1" ]]; then
  log "SKIP_FLASH=1, skipping Arduino flash"
elif [[ ! -e "$ARDUINO_PORT" ]]; then
  log "WARN: $ARDUINO_PORT not present, skipping Arduino flash"
elif ! command -v "$ARDUINO_CLI" >/dev/null 2>&1; then
  log "WARN: arduino-cli not found on PATH, skipping flash"
else
  log "compiling $SKETCH_DIR ($ARDUINO_FQBN)"
  "$ARDUINO_CLI" compile --fqbn "$ARDUINO_FQBN" "$SKETCH_DIR"
  log "uploading to $ARDUINO_PORT"
  "$ARDUINO_CLI" upload -p "$ARDUINO_PORT" --fqbn "$ARDUINO_FQBN" "$SKETCH_DIR"
  # Give the Uno time to reset out of bootloader before anything else opens the port.
  sleep 2
fi

log "starting timelapse"
# `exec` replaces this shell so systemd sees the python process as the unit's main PID.
# shellcheck disable=SC2086
exec python3 "$REPO_DIR/pi/timelapse.py" $TIMELAPSE_ARGS
