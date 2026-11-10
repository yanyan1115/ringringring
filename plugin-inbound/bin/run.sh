#!/usr/bin/env bash
# Launcher for the incoming-call plugin: reads inbound.env (the SIP account that receives calls)
# and voice.env (the user's own SIP address, only used to recognize the caller), then sets up the pjsua2 environment.
set -uo pipefail
PREFIX="${RINGRINGRING_HOME:-$HOME/.local/share/ringringring}"
load() {
  [ -f "$1" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line#"${line%%[![:space:]]*}"}"; line="${line#export }"
    case "$line" in "" | "#"*) continue ;; esac
    k="${line%%=*}"; v="${line#*=}"
    [[ "$k" =~ ^[A-Z_][A-Z0-9_]*$ ]] || continue
    case "$v" in \"*\") v="${v:1:${#v}-2}" ;; \'*\') v="${v:1:${#v}-2}" ;; esac
    export "$k=$v"
  done < "$1"
}
load "$HOME/.config/ringringring/voice.env"
load "$HOME/.config/ringringring/inbound.env"
export PYTHONPATH="$PREFIX/pjsua2/python:$PREFIX/app"
export LD_LIBRARY_PATH="$PREFIX/pjsua2/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PATH="$PREFIX/venv/bin:$HOME/.local/bin:$PATH"
export VOICE_RTP_PORT="${VOICE_INBOUND_RTP_PORT:-4100}"   # the outgoing-call plugin uses 4000
if [ -z "${VOICE_TTS_CMD:-}" ] && [ -z "${VOICE_TTS:-}" ]; then export VOICE_TTS=edge; fi
export VOICE_DIAG_FILE="${XDG_STATE_HOME:-$HOME/.local/state}/ringringring/inbound.log"
exec "$PREFIX/venv/bin/python" "$(dirname "$0")/inbound_channel.py"
