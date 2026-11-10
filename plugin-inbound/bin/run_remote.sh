#!/usr/bin/env bash
# Launcher for the HTTPS remote MCP (for systemd). Secrets live in an EnvironmentFile, never here.
set -uo pipefail
PREFIX="${RINGRINGRING_HOME:-$HOME/.local/share/ringringring}"
source <(sed -n '/^load() {/,/^}/p' "$(dirname "$0")/run.sh")
load "$HOME/.config/ringringring/voice.env"
load "$HOME/.config/ringringring/inbound.env"
# Which account dials out (must come after load: load overrides same-named variables from a systemd EnvironmentFile)
#   inbound (default) = the inbound.env account; voice = VOICE_SIP_ID/USER/PASS from voice.env (calls your own address)
if [ "${VOICE_REMOTE_ACCOUNT:-inbound}" = "voice" ]; then
  export VOICE_INBOUND_SIP_ID="${VOICE_SIP_ID:-}" VOICE_INBOUND_SIP_USER="${VOICE_SIP_USER:-}" VOICE_INBOUND_SIP_PASS="${VOICE_SIP_PASS:-}"
fi
# The caller-ID name is filled in by the model in dial; here we only clear the default name voice.env sets for the plugin
export VOICE_DISPLAY_NAME="${VOICE_REMOTE_DISPLAY_NAME:-}"
export PYTHONPATH="$PREFIX/pjsua2/python:$PREFIX/app"
export LD_LIBRARY_PATH="$PREFIX/pjsua2/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PATH="$PREFIX/venv/bin:$HOME/.local/bin:$PATH"
if [ -z "${VOICE_TTS_CMD:-}" ] && [ -z "${VOICE_TTS:-}" ]; then export VOICE_TTS=edge; fi
exec "$PREFIX/venv/bin/python" "$(dirname "$0")/remote_http.py" "$@"
