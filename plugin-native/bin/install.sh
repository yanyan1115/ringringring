#!/usr/bin/env bash
# One-time install for the ringringring plugin (Linux x86_64, Python 3.11).
#
#   install.sh --pjsua2 <tarball path or https URL>   [--src <ringback checkout>] [--no-apt]
#   install.sh --check     only report missing system packages; changes nothing
#   (--no-apt: never run apt-get, even as root -- stop and list what is missing)
#
# Puts everything under $RINGRINGRING_HOME (default ~/.local/share/ringringring):
#   pjsua2/   prebuilt pjsua2 Python bindings + shared libs (from the tarball)
#   app/      the voice MCP code, copied from the ringback checkout
#   venv/     Python deps (mcp, httpx, edge-tts)
# and creates an empty ~/.config/ringringring/voice.env (mode 600) if there is none.
# Re-running is safe: it replaces pjsua2/ and app/, and reuses venv/.
# No credentials are read or written here.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="${RINGRINGRING_HOME:-$HOME/.local/share/ringringring}"
CONF="${RINGRINGRING_CONF:-$HOME/.config/ringringring/voice.env}"
PYTHON_BIN="${PYTHON_BIN:-python3.11}"
SRC="$(cd "$HERE/.." && pwd)"
TARBALL="${PJSUA2_TARBALL:-}"
CHECK=0
NO_APT=0

while [ $# -gt 0 ]; do
  case "$1" in
    --pjsua2) TARBALL="$2"; shift 2 ;;
    --src)    SRC="$2"; shift 2 ;;
    --check)  CHECK=1; shift ;;
    --no-apt) NO_APT=1; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say() { echo "[ringringring] $*"; }
die() { echo "[ringringring] $*" >&2; exit 1; }

[ "$(uname -s)-$(uname -m)" = "Linux-x86_64" ] || die "prebuilt pjsua2 is Linux x86_64 only for now."
command -v "$PYTHON_BIN" >/dev/null || die "$PYTHON_BIN not found (the prebuilt bindings are cp311). Set PYTHON_BIN."
"$PYTHON_BIN" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 11))' \
  || die "$PYTHON_BIN is not Python 3.11."
# --- system runtime libs -----------------------------------------------------------
missing=()
# ldconfig lives in /sbin, which is not on a normal user's PATH on Debian
LDCONFIG="$(command -v ldconfig || echo /sbin/ldconfig)"
libs="$("$LDCONFIG" -p)"   # captured once: `ldconfig -p | grep -q` trips pipefail on SIGPIPE
for so in libssl.so.3 libasound.so.2 libopus.so.0 libuuid.so.1; do
  grep -qF "$so" <<<"$libs" || missing+=("$so")
done
command -v ffmpeg >/dev/null || missing+=(ffmpeg)
"$PYTHON_BIN" -c 'import venv, ensurepip' 2>/dev/null || missing+=(python3.11-venv)
pkgs="libssl3 libasound2 libopus0 libuuid1 ffmpeg python3.11-venv"
if [ "$CHECK" = 1 ]; then
  if [ ${#missing[@]} -gt 0 ]; then
    say "missing: ${missing[*]} (Debian/Ubuntu: apt-get install $pkgs)"; exit 1
  fi
  say "system packages OK"; exit 0
fi
[ -n "$TARBALL" ] || die "pass --pjsua2 <tarball path or URL> (pjsua2-2.17-linux-x86_64-cp311.tar.gz)."
[ -f "$SRC/voice_mcp.py" ] || die "ringback code not found at $SRC (use --src)."
if [ ${#missing[@]} -gt 0 ]; then
  if [ "$NO_APT" = 0 ] && [ "$(id -u)" = 0 ] && command -v apt-get >/dev/null; then
    say "installing runtime packages (missing: ${missing[*]})"
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q $pkgs >/dev/null
  else
    die "missing: ${missing[*]}. Install with: sudo apt-get install $pkgs"
  fi
fi

mkdir -p "$PREFIX"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

# --- pjsua2 ------------------------------------------------------------------------
case "$TARBALL" in
  https://*)
    say "downloading pjsua2"
    curl -fsSL "$TARBALL" -o "$WORK/pj.tar.gz"
    curl -fsSL "$TARBALL.sha256" -o "$WORK/pj.sha256" 2>/dev/null || true ;;
  *)
    [ -f "$TARBALL" ] || die "no such file: $TARBALL"
    cp "$TARBALL" "$WORK/pj.tar.gz"
    [ -f "$TARBALL.sha256" ] && cp "$TARBALL.sha256" "$WORK/pj.sha256" || true ;;
esac
if [ -s "$WORK/pj.sha256" ]; then
  want="$(awk '{print $1; exit}' "$WORK/pj.sha256")"
  got="$(sha256sum "$WORK/pj.tar.gz" | awk '{print $1}')"
  [ "$want" = "$got" ] || die "pjsua2 tarball checksum mismatch."
  say "checksum ok"
else
  say "warning: no .sha256 next to the tarball, skipping checksum"
fi
mkdir -p "$WORK/pj"
tar -xzf "$WORK/pj.tar.gz" -C "$WORK/pj"
top="$(find "$WORK/pj" -mindepth 1 -maxdepth 1 -type d | head -1)"
[ -d "$top/python" ] && [ -d "$top/lib" ] || die "tarball layout unexpected (want python/ and lib/)."
rm -rf "$PREFIX/pjsua2"; mv "$top" "$PREFIX/pjsua2"

# --- app code ----------------------------------------------------------------------
rm -rf "$PREFIX/app"; mkdir -p "$PREFIX/app/tools"
# sound_events.py is imported at top level by voice_agent.py: it must be in this list.
# The sound label hook is off unless VOICE_SOUND_EVENTS/URL/KEY are set; no classifier ships here.
for f in voice_mcp.py voice_agent.py privacy.py platform_compat.py aec.py \
         sound_events.py; do
  [ -f "$SRC/$f" ] && cp "$SRC/$f" "$PREFIX/app/"
done
cp "$SRC/tools/edge_tts_wav.sh" "$PREFIX/app/tools/"
chmod +x "$PREFIX/app/tools/edge_tts_wav.sh"

# --- venv --------------------------------------------------------------------------
[ -x "$PREFIX/venv/bin/python" ] || "$PYTHON_BIN" -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install -q --upgrade pip
"$PREFIX/venv/bin/pip" install -q "mcp>=1.2,<2" "httpx>=0.27" edge-tts

# --- config skeleton (no values) ---------------------------------------------------
if [ ! -f "$CONF" ]; then
  mkdir -p "$(dirname "$CONF")"
  ( umask 077; cat > "$CONF" <<'CONFEOF'
# ringringring settings. Anything set in the plugin config wins over this file.
# Fill in with an editor; keep this file mode 600.
VOICE_SIP_ID=
VOICE_SIP_USER=
VOICE_SIP_PASS=
# Speech recognition: ElevenLabs recommended, Groq (VOICE_STT_KEY) is the fallback
# VOICE_STT_11LABS_KEY=   (recommended; uncomment and fill in to enable)
VOICE_STT_KEY=
# VOICE_STT_URL=https://api.groq.com/openai/v1/audio/transcriptions
# VOICE_STT_MODEL=whisper-large-v3-turbo
VOICE_STT_LANG=zh
VOICE_EDGE_VOICE=zh-CN-YunxiNeural
VOICE_STUN=stun.linphone.org:3478
VOICE_END_SILENCE=1.0
CONFEOF
  )
  say "created $CONF (empty, fill it in or use the plugin config)"
fi

# --- smoke test --------------------------------------------------------------------
PYTHONPATH="$PREFIX/pjsua2/python:$PREFIX/app" LD_LIBRARY_PATH="$PREFIX/pjsua2/lib" \
  "$PREFIX/venv/bin/python" -c '
import pjsua2 as pj, mcp, httpx
ep = pj.Endpoint(); ep.libCreate()
print("[ringringring] pjsua2", ep.libVersion().full, "ok")
ep.libDestroy()
' 2>&1 | grep '^\[ringringring\]' || die "smoke test failed: pjsua2 did not load."
say "installed to $PREFIX"
