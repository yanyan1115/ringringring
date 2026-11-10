"""Voice call engine for ringback-voice: pjsua2 + say (TTS) + whisper.cpp (STT).

Provides CallSession: place a SIP call to the Linphone account, then dynamically
speak() arbitrary text and listen() to the caller (transcribed). The MCP wraps
this so a Claude session can drive a phone conversation turn by turn.

Run env (set by the launcher):
  PYTHONPATH   -> pjsua2 build lib dir
  DYLD_LIBRARY_PATH -> pjproject dylibs + openssl
"""
from __future__ import annotations

import atexit
import base64
import io
import json
import math
import os
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave

import pjsua2 as pj
try:                                   # native crashes leave a stack in the diag log
    import faulthandler as _fh
    if os.environ.get("VOICE_DIAG_FILE"):
        _fh_file = open(os.environ["VOICE_DIAG_FILE"], "a")
        _fh.enable(file=_fh_file, all_threads=True)
except Exception:
    pass
import sound_events
from privacy import redact, install_stderr, Redactor

install_stderr()   # stray prints / logging / tracebacks on stderr get scrubbed too

# platform_compat lives next to this file; make it importable no matter how we're
# loaded (the test harness loads voice_agent.py by path without adding its dir to path).
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from platform_compat import IS_MAC, IS_WIN, detached_popen_kwargs, synthesize_to_wav  # noqa: E402

# ---- config (all via env; see voice.env.example) ------------------------------
# Your SIP identity/credentials come from the environment (the launcher sources
# voice.env). No personal account is baked into this file.
SIP_ID = os.environ.get("VOICE_SIP_ID", "sip:user@sip.linphone.org")
SIP_CALLEE = os.environ.get("VOICE_SIP_CALLEE", os.environ.get("VOICE_SIP_ID",
                                                               "sip:user@sip.linphone.org"))
SIP_USER = os.environ.get("VOICE_SIP_USER", "user")
SIP_PASS = os.environ.get("VOICE_SIP_PASS", "")
SIP_PROXY = os.environ.get("VOICE_SIP_PROXY", "sip:sip.linphone.org;transport=tls")
# Caller-ID display name shown on the phone (the SIP From header display name).
SIP_DISPLAY_NAME = os.environ.get("VOICE_DISPLAY_NAME", "").strip()
WHISPER_BIN = os.environ.get("WHISPER_BIN", "whisper-cli")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL",
                               os.path.expanduser("~/.whisper-models/ggml-small.en.bin"))
FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")

# Voice-activity thresholds. Background noise was falsely triggering barge-in, so
# these are now (a) env-tunable and (b) raised at call time by a measured noise floor
# (CallSession.place_call -> _measure_rms). Real speech must clear the effective
# threshold = clamp(max(base, noise_floor * NOISE_FACTOR), .., RMS_CAP).
BARGE_RMS_BASE = float(os.environ.get("VOICE_BARGE_RMS", "550"))
LISTEN_RMS_BASE = float(os.environ.get("VOICE_LISTEN_RMS", "320"))
NOISE_FACTOR = float(os.environ.get("VOICE_NOISE_FACTOR", "2.5"))
RMS_CAP = float(os.environ.get("VOICE_RMS_CAP", "3000"))  # never raise threshold above this
# Barge-in threshold floor. The barge detector must key off the near-end level DURING our
# TX (when the user is silent), NOT the pre-call ambient noise floor: a real harvested call
# measured ambient ~2441 but the during-TX floor ~1-800 and the user's barge ~2000-3972 —
# so a noise-floor-derived threshold (capped at 3000) MISSED the user. We measure the live
# during-TX floor each utterance and clamp it to at least this, well under a real barge.
# 1300 -> 700 (10-26): the caller's phone-side speech mostly peaks 700-1500, so 1300 missed real barges
BARGE_RMS_MIN = float(os.environ.get("VOICE_BARGE_RMS_MIN", "700"))
# Listen (capturing the user) uses a GENTLER factor than barge: barge must avoid
# false-triggering on noise/echo (high bar), but listen must still hear the user in a
# noisy room (lower bar) — whisper discards any non-speech that slips through.
LISTEN_NOISE_FACTOR = float(os.environ.get("VOICE_LISTEN_NOISE_FACTOR", "1.5"))
# Debounce: consecutive over-threshold polls required to count as real speech (not a
# transient). Higher = more noise-robust, slightly less instant barge-in.
BARGE_DEBOUNCE = int(os.environ.get("VOICE_BARGE_DEBOUNCE", "5"))    # ~0.40s at 0.08s/poll
LISTEN_DEBOUNCE = int(os.environ.get("VOICE_LISTEN_DEBOUNCE", "3"))  # ~0.30s at 0.10s/poll

# Barge-in is ON by default. A harvested speakerphone call (user 100% on speaker) showed
# essentially NO echo in the near-end — modern phones run their own acoustic echo
# cancellation, so our TTS does not loop back. Barge-in is therefore safe without AEC.
# The early-barge guard below stays as a FALLBACK: if a device ever does echo our voice
# back, a sustained "barge" in the first EARLY_BARGE_SEC flips that call to half-duplex.
# Set VOICE_HALF_DUPLEX=1 to force half-duplex (a known echoey device / no phone AEC).
HALF_DUPLEX = os.environ.get("VOICE_HALF_DUPLEX", "0").strip().lower() in ("1", "true", "yes")
# A sustained "barge" within the first EARLY_BARGE_SEC of our speech is treated as echo
# (a real person rarely cuts in this early) and flips the call to half-duplex as a safety.
# Shorter now (0.8s): with phone-side AEC there's no echo to catch, and a real early barge
# should register — so only the very start is guarded.
EARLY_BARGE_SEC = float(os.environ.get("VOICE_EARLY_BARGE_SEC", "0.8"))
# Early "echo" verdicts needed in one call before it flips to half-duplex. One early hit used
# to kill barge-in for the whole call; a quick real interrupter tripped it (10-26).
ECHO_STRIKES = int(os.environ.get("VOICE_ECHO_STRIKES", "2"))
POST_SPEAK_DRAIN = float(os.environ.get("VOICE_POST_SPEAK_DRAIN", "0.35"))  # let echo tail clear
# Turn-taking — TWO phases, because one tight timeout dropped real speech:
#   START_TIMEOUT: how long to wait for the user's FIRST word before giving up. Must be
#     generous — a 2.0s window guillotined anyone who paused to think before answering
#     (reproduced offline in tests/test_capture.py: user starts at 2.5s -> captured "").
#   END_SILENCE: once they HAVE spoken, end the turn this long after their last word —
#     this is the responsive "stop after ~1.5s of no new words" endpoint.
START_TIMEOUT = float(os.environ.get("VOICE_START_TIMEOUT", "4.0"))
END_SILENCE = float(os.environ.get("VOICE_END_SILENCE", "1.5"))
# per-turn recording cap, counted from when listening starts (was a hard 15s that cut long stories off)
LISTEN_MAX_SEC = float(os.environ.get("VOICE_LISTEN_MAX_SEC", "40"))
# End-of-turn is detected on AUDIO ENERGY, not whisper text: the user is "still talking"
# while the near-end tail RMS is above this OR new words keep appearing. Keying purely off
# the transcript hung for ~15s on real phone audio (whisper re-words the same clip every
# poll, resetting the timer) and cutting on word-count plateaus chopped off real speech.
# In-call silence measured ~150 vs speech ~2000+, so this cleanly separates them.
LISTEN_END_RMS = float(os.environ.get("VOICE_LISTEN_END_RMS", "600"))
# legacy alias (older callers): treat NO_SPEECH_SEC as the start timeout
NO_SPEECH_SEC = float(os.environ.get("VOICE_NO_SPEECH_SEC", str(START_TIMEOUT)))

# Persistent whisper.cpp HTTP server: loads the model ONCE so each transcription is fast
# inference (~0.1s) instead of a fresh whisper-cli model reload (~0.5-1.5s) — this is what
# makes streaming capture (transcribe a window ~3x/sec) fast enough. Lazy: started on the
# first transcription of a call; reaped after WHISPER_SERVER_IDLE_SEC idle to free memory.
# Falls back to whisper-cli if it can't start.
WHISPER_SERVER_BIN = os.environ.get("WHISPER_SERVER_BIN", "whisper-server")
WHISPER_SERVER_MODEL = os.environ.get("WHISPER_SERVER_MODEL",
                                      os.path.expanduser("~/.whisper-models/ggml-base.en.bin"))
WHISPER_SERVER_HOST = os.environ.get("WHISPER_SERVER_HOST", "127.0.0.1")
WHISPER_SERVER_PORT = int(os.environ.get("WHISPER_SERVER_PORT", "8642"))
WHISPER_SERVER_IDLE_SEC = float(os.environ.get("WHISPER_SERVER_IDLE_SEC", "300"))  # GC after 5 min idle

# ---- silence diagnostics --------------------------------------------------------------
# Every listen turn ends with ONE summary line on stderr (the stdio MCP's stderr lands in
# the client's MCP log), so a [SILENCE] result is attributable after the fact:
#   wav=44B            -> no RTP audio ever arrived (media/network problem, not the user)
#   rms_max < thresh   -> audio arrived but never crossed the speech-energy threshold
#   voiced>0 text=0w   -> speech energy was seen but whisper produced no usable words
# VOICE_DEBUG=1 adds the chatty detail (each transcript the hallucination filter discards).
# VOICE_DEBUG_KEEP_WAV=1 keeps the recorded audio of a silent turn under VOICE_DEBUG_WAV_DIR
# so you can listen to what actually arrived.
VOICE_DEBUG = os.environ.get("VOICE_DEBUG", "0").strip().lower() in ("1", "true", "yes")
DEBUG_KEEP_WAV = os.environ.get("VOICE_DEBUG_KEEP_WAV", "0").strip().lower() in ("1", "true", "yes")
DEBUG_WAV_DIR = os.environ.get("VOICE_DEBUG_WAV_DIR",
                               os.path.join(tempfile.gettempdir(), "ringback-debug"))


# VOICE_DIAG_FILE: also append _dlog lines (redacted; listen summaries are numbers only)
# to a file, because the host's MCP log stops capturing stderr after the handshake.
DIAG_FILE = os.environ.get("VOICE_DIAG_FILE", "").strip()

# Quiet listen: while a call is up, keep recording the caller's side in the background so the
# model can stay silent and still hear them (breathing, small sounds) between turns.
# Default OFF: it records the call continuously (kept in memory/temp files, dropped at hang-up).
QUIET_LISTEN = os.environ.get("VOICE_QUIET_LISTEN", "0").strip().lower() in ("1", "true", "yes")
QUIET_CHUNK_SEC = float(os.environ.get("VOICE_QUIET_CHUNK_SEC", "10"))
QUIET_MAX_SEC = float(os.environ.get("VOICE_QUIET_MAX_SEC", "600"))   # keep at most the last 10 min
QUIET_VOICED_SEC = float(os.environ.get("VOICE_QUIET_VOICED_SEC", "0.3"))  # loud time a chunk needs
# Quiet-listen gate base (breathing / soft sounds fall under the speech gate).
# Unset = same as VOICE_LISTEN_RMS (old behaviour). Speech/barge gates are not touched.
QUIET_RMS_BASE = float(os.environ.get("VOICE_QUIET_RMS", "") or LISTEN_RMS_BASE)

# Push-on-voice (needs quiet listen): a sentry thread tails the background recording while a
# call is up and pushes a channel notification the moment the caller makes a sound, so the model
# does not have to fetch (quiet_listen), never has to speak, and is never looking at the past.
PUSH_ON_VOICE = os.environ.get("VOICE_PUSH_ON_VOICE", "0").strip().lower() in ("1", "true", "yes")
PUSH_TICK = float(os.environ.get("VOICE_PUSH_TICK", "3"))            # how often the sentry looks
PUSH_GAP_SEC = float(os.environ.get("VOICE_PUSH_GAP_SEC", "2"))      # silence that ends a stretch
PUSH_MIN_INTERVAL = float(os.environ.get("VOICE_PUSH_MIN_INTERVAL", "15"))  # re-push floor, by feel

WAIT_VOICE_TICK = float(os.environ.get("VOICE_WAIT_TICK", "0.3"))   # wait_for_voice poll
WAIT_VOICE_SEC = float(os.environ.get("VOICE_WAIT_VOICED_SEC", "0.15"))  # loud time that ends a wait

PUSH_SINK = None   # set by the MCP server (inbound_channel.emit); None = pushes are dropped


def set_push_sink(fn) -> None:
    """Let the MCP server hand us its channel emitter. Kept as a plain global so voice_agent
    never imports the server (the server imports us)."""
    global PUSH_SINK
    PUSH_SINK = fn


def _dlog(msg: str) -> None:
    line = f"[ringback] {redact(msg)}"
    print(line, file=sys.stderr, flush=True)
    if DIAG_FILE:
        try:
            fd = os.open(DIAG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as fh:
                fh.write(time.strftime("%Y-%m-%dT%H:%M:%S ") + line + "\n")
        except OSError:
            pass


class _RedactingLogWriter(pj.LogWriter):
    """pjsua log sink: every line passes through privacy.redact() before it hits
    stderr or VOICE_LOG_FILE, so raw SIP/SDP (phone IPs, candidates, SRTP keys)
    never lands on disk by default. VOICE_LOG_RAW=1 bypasses this (local debugging
    only; never hand that file to a model)."""

    def __init__(self, path: str = "", raw: bool = False):
        super().__init__()
        self.raw = raw
        self._r = Redactor()      # one per writer: folded-header state survives across entries
        self.fh = open(path, "a", encoding="utf-8") if path else None
        if self.fh:
            os.chmod(path, 0o600)

    def write(self, entry):
        msg = entry.msg if self.raw else self._r.feed(entry.msg)
        out = self.fh or sys.stderr
        out.write(msg if msg.endswith("\n") else msg + "\n")
        out.flush()


def _vlog(msg: str) -> None:
    if VOICE_DEBUG:
        _dlog(msg)


def _eff_threshold(base: float, noise_floor: float, factor: float = NOISE_FACTOR) -> float:
    """Speech threshold = base, raised to clear measured ambient noise, then capped.

    `factor` is how far above the noise floor speech must be — high for barge-in
    (don't false-trigger), gentler for listen (still hear the user in a noisy room).
    """
    return min(max(base, noise_floor * factor), RMS_CAP)


def _tts_to_wav(text: str) -> str:
    """Render text -> 16 kHz mono 16-bit WAV (pjsua2 resamples as needed). Engine is
    Piper by default, falling back to the OS-native voice; see platform_compat."""
    wav = tempfile.mktemp(suffix=".wav")
    return synthesize_to_wav(text, wav)


def _wav_duration(path: str) -> float:
    with wave.open(path, "rb") as w:
        return w.getnframes() / float(w.getframerate())


def _wav_snapshot(src: str) -> str:
    """Rewrite a still-being-recorded WAV with a CORRECT length header so a transcriber
    sees ALL audio captured so far. An in-progress pjsua recorder leaves the data-size
    field stale (set at close), so reading it directly yields only a fragment. Returns a
    temp path (caller removes it) or "" if there's no audio yet."""
    try:
        with open(src, "rb") as f:
            head = f.read(44)
            if len(head) < 44 or head[:4] != b"RIFF":
                return ""
            ch = struct.unpack_from("<H", head, 22)[0] or 1
            sr = struct.unpack_from("<I", head, 24)[0]
            bits = struct.unpack_from("<H", head, 34)[0] or 16
            pcm = f.read()                       # everything written so far, past the header
    except OSError:
        return ""
    if not pcm or sr == 0:
        return ""
    dst = tempfile.mktemp(suffix=".wav")
    with wave.open(dst, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(bits // 8)
        w.setframerate(sr)
        w.writeframes(pcm)
    return dst


def _rm(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _cut_spoken(text: str, frac: float):
    """Split `text` at fraction `frac` of its playback: by word when it has spaces
    (English), by character otherwise (Chinese has no spaces — splitting on them
    reported the whole sentence as said)."""
    frac = min(1.0, max(0.0, frac))
    words = text.split()
    if len(words) > 1:
        k = max(1, int(round(len(words) * frac)))
        return " ".join(words[:k]), " ".join(words[k:])
    k = max(1, int(round(len(text) * frac)))
    return text[:k], text[k:]


# Whisper emits these bare phrases as HALLUCINATIONS on silence / non-speech audio. If they
# slip through they look like the user spoke — flipping the turn into "they answered" and
# then ending it 1.5s later, swallowing the real reply that follows. Matched only as the
# WHOLE transcript (so "okay thanks for the help" is kept; a lone "Thanks for watching." is
# dropped). Deliberately excludes real short answers like yes/no/okay/sure.
_HALLUCINATIONS = {
    "you", "thank you", "thanks", "thanks for watching", "thank you for watching",
    "thank you.", "thanks for watching.", "bye", "bye.", "you're welcome",
    "please subscribe", "subscribe", ".", "so",
    "谢谢观看", "谢谢大家", "谢谢", "请不吝点赞 订阅 转发 打赏支持明镜与点点栏目",
    "字幕由amara.org社区提供", "优优独播剧场——yoyo television series exclusive",
}


# Bare acknowledgements ("嗯", "um") are BOTH a real short answer and something whisper
# invents on silence. _clean_text keeps them; whoever knows the near-end energy decides:
# kept if the user's audio actually crossed the speech threshold, dropped otherwise.
_BACKCHANNELS = {"嗯", "嗯嗯", "uh", "um"}


def _is_backchannel(text: str) -> bool:
    return text.strip(" .,!?-。！？，、…").lower() in _BACKCHANNELS


def _clean_text(text: str) -> str:
    """Normalize whitespace and drop whisper non-speech artifacts (bracketed annotations
    like [BLANK_AUDIO]/(silence), and bare silence-hallucinations). Returns "" if nothing
    real was said."""
    text = " ".join(text.split()).strip()
    if not text:
        return ""
    if text.startswith(("[", "(")) and text.endswith(("]", ")")):
        _vlog(f"transcript filtered as non-speech annotation: {text!r}")
        return ""
    if text.strip(" .,!?-。！？，、…").lower() in _HALLUCINATIONS:
        _vlog(f"transcript filtered as silence-hallucination: {text!r}")
        return ""
    return text


def _transcribe(wav: str) -> str:
    out = subprocess.run([WHISPER_BIN, "-m", WHISPER_MODEL, "-f", wav, "-nt", "-t", "8"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        _dlog(f"whisper-cli failed (rc={out.returncode}): {out.stderr.strip()[-200:]}")
    return _clean_text(out.stdout)


# --- persistent whisper-server: load model once (lazy), reap after idle ---------------
import threading

_WSRV_URL = f"http://{WHISPER_SERVER_HOST}:{WHISPER_SERVER_PORT}"
_wsrv_proc = None
_wsrv_last_use = 0.0
_wsrv_lock = threading.Lock()
_wsrv_reaper = None
_wsrv_down_logged = False   # log the CLI fallback once per outage, not on every 0.3s poll


def _wsrv_health() -> bool:
    try:
        urllib.request.urlopen(_WSRV_URL + "/", timeout=1)
        return True
    except urllib.error.HTTPError:
        return True       # server answered (e.g. 404) -> it's up
    except Exception:
        return False      # connection refused / timeout -> down


def _wsrv_kill_strays():
    """Kill any whisper-server already on our port that ISN'T ours — i.e. an orphan left by
    a previous (dead) MCP process. POSIX best-effort; called before re-spawning."""
    if IS_WIN:
        return
    try:
        out = subprocess.run(["pgrep", "-f", f"whisper-server.*--port {WHISPER_SERVER_PORT}"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return
    keep = {os.getpid(), _wsrv_proc.pid if _wsrv_proc is not None else -1}
    for tok in out.split():
        try:
            pid = int(tok)
        except ValueError:
            continue
        if pid not in keep:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass


def _wsrv_terminate():
    """Stop the whisper-server (and its watchdog) — the whole process group on POSIX so
    nothing is left behind. Caller holds _wsrv_lock (or it's shutdown). Lock-free itself."""
    global _wsrv_proc
    p, _wsrv_proc = _wsrv_proc, None
    if p is None:
        return
    try:
        if IS_WIN:
            p.terminate()
        else:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
    except Exception:
        try:
            p.terminate()
        except Exception:
            pass


def _wsrv_reaper_loop():
    while True:
        time.sleep(20)
        with _wsrv_lock:
            if _wsrv_proc is None:
                return
            if time.time() - _wsrv_last_use > WHISPER_SERVER_IDLE_SEC:
                _wsrv_terminate()   # idle too long -> free it (kills the whole group)
                return              # a future call re-spawns + re-arms


def _wsrv_spawn():
    """Start the server if it isn't up (non-blocking) and arm the idle reaper.

    The server's lifetime is tied to THIS process: on POSIX it runs under a tiny `sh`
    watchdog that kills it the moment our PID is gone, so a dead/killed MCP can NEVER leave
    an orphaned whisper-server. (The previous design detached the server and kept the reaper
    only in the parent — when the parent died, the reaper died with it and the server ran
    forever; observed as a 2.75-day-old orphan with PPID=1.)"""
    global _wsrv_proc, _wsrv_reaper, _wsrv_last_use
    with _wsrv_lock:
        if _wsrv_health() or (_wsrv_proc is not None and _wsrv_proc.poll() is None):
            return
        _wsrv_kill_strays()   # clear any orphan from a previous dead MCP before re-spawning
        try:
            if IS_WIN:
                _wsrv_proc = subprocess.Popen(
                    [WHISPER_SERVER_BIN, "-m", WHISPER_SERVER_MODEL,
                     "--host", WHISPER_SERVER_HOST, "--port", str(WHISPER_SERVER_PORT)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    **detached_popen_kwargs())
            else:
                # sh watchdog: run the server, then exit (killing it) once our PID is gone.
                # Own session so the reaper / atexit can killpg the whole group.
                watchdog = ('"$1" -m "$2" --host "$3" --port "$4" & srv=$!; '
                            f'while kill -0 {os.getpid()} 2>/dev/null; do sleep 5; done; '
                            'kill "$srv" 2>/dev/null')
                _wsrv_proc = subprocess.Popen(
                    ["/bin/sh", "-c", watchdog, "sh", WHISPER_SERVER_BIN, WHISPER_SERVER_MODEL,
                     WHISPER_SERVER_HOST, str(WHISPER_SERVER_PORT)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True)
            _wsrv_last_use = time.time()
        except FileNotFoundError:
            _wsrv_proc = None
            return
        if _wsrv_reaper is None or not _wsrv_reaper.is_alive():
            _wsrv_reaper = threading.Thread(target=_wsrv_reaper_loop, daemon=True)
            _wsrv_reaper.start()


# On clean interpreter exit, take the whisper-server down too (belt-and-suspenders alongside
# the sh watchdog, which also covers SIGKILL where atexit can't run).
atexit.register(_wsrv_terminate)


def _wsrv_warm():
    """Kick off the model load now (e.g. when a call connects) without blocking."""
    if not _wsrv_health():
        _wsrv_spawn()


def _wsrv_ready(wait_sec: float = 12.0) -> bool:
    if _wsrv_health():
        return True
    _wsrv_spawn()
    end = time.time() + wait_sec
    while time.time() < end:
        if _wsrv_health():
            return True
        time.sleep(0.2)
    return False


def _transcribe_stream(wav: str) -> str:
    """Fast transcription via the persistent whisper-server; whisper-cli fallback."""
    global _wsrv_last_use, _wsrv_down_logged
    if not _wsrv_ready():
        if not _wsrv_down_logged:
            _dlog("whisper-server not ready -> falling back to whisper-cli (slower)")
            _wsrv_down_logged = True
        return _transcribe(wav)
    try:
        with open(wav, "rb") as f:
            audio = f.read()
        b = "----rbkboundary"
        body = (
            (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\n'
             f'Content-Type: audio/wav\r\n\r\n').encode() + audio +
            (f'\r\n--{b}\r\nContent-Disposition: form-data; name="response_format"\r\n\r\n'
             f'text\r\n--{b}--\r\n').encode())
        req = urllib.request.Request(
            _WSRV_URL + "/inference", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={b}"})
        with urllib.request.urlopen(req, timeout=6) as r:
            text = r.read().decode("utf-8", "ignore")
        _wsrv_last_use = time.time()
        _wsrv_down_logged = False
        return _clean_text(text)
    except Exception as e:
        if not _wsrv_down_logged:
            _dlog(f"whisper-server inference failed ({type(e).__name__}: {str(e)[:120]}) "
                  "-> falling back to whisper-cli")
            _wsrv_down_logged = True
        return _transcribe(wav)


# --- Cloud STT (any OpenAI-compatible /audio/transcriptions; Groq by default) ----------
# Opt-in: set VOICE_STT_KEY (or VOICE_STT_ENV_FILE -> a KEY=VALUE file holding it). Point
# VOICE_STT_URL / VOICE_STT_MODEL at another provider to swap the "ears". The older
# GROQ_API_KEY / VOICE_GROQ_* names are still honoured. Then each
# turn is transcribed ONCE by Groq after the user stops talking; the in-turn streaming polls
# are skipped (free tier is ~20 req/min, 3 polls/sec would burn it) and the endpoint is driven
# by near-end energy alone. Any Groq failure falls back to the local whisper path.
def _env_first(*names: str, default: str = "") -> str:
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return default


GROQ_STT_URL = _env_first("VOICE_STT_URL", "VOICE_GROQ_URL",
                          default="https://api.groq.com/openai/v1/audio/transcriptions")
GROQ_STT_MODEL = _env_first("VOICE_STT_MODEL", "VOICE_GROQ_MODEL",
                            default="whisper-large-v3-turbo")
STT_LANG = os.environ.get("VOICE_STT_LANG", "zh").strip()


def _groq_key() -> str:
    key = _env_first("VOICE_STT_KEY", "GROQ_API_KEY")
    path = _env_first("VOICE_STT_ENV_FILE", "VOICE_GROQ_ENV_FILE")
    if not key and path:
        try:
            with open(os.path.expanduser(path)) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("export "):
                        line = line[7:]
                    if line.startswith(("VOICE_STT_KEY=", "GROQ_API_KEY=")):
                        key = line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError as e:
            _dlog(f"STT env file unreadable ({type(e).__name__})")
    return key


_GROQ_KEY = _groq_key()
USE_GROQ = bool(_GROQ_KEY)


def _groq_transcribe(wav: str) -> str:
    with open(wav, "rb") as f:
        audio = f.read()
    b = "----rbkgroqboundary"
    fields = {"model": GROQ_STT_MODEL, "response_format": "text", "temperature": "0"}
    if STT_LANG:
        fields["language"] = STT_LANG
    prompt = os.environ.get("VOICE_STT_PROMPT", "").strip()   # names/vocab hint
    if prompt:
        fields["prompt"] = prompt
    body = b"".join(
        (f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n').encode()
        for k, v in fields.items())
    body += (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\n'
             f'Content-Type: audio/wav\r\n\r\n').encode() + audio + f'\r\n--{b}--\r\n'.encode()
    req = urllib.request.Request(
        GROQ_STT_URL, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={b}",
                 "Authorization": f"Bearer {_GROQ_KEY}",
                 "User-Agent": "ringback-voice"})
    with urllib.request.urlopen(req, timeout=float(_env_first("VOICE_STT_TIMEOUT", "VOICE_GROQ_TIMEOUT", default="8"))) as r:
        return _clean_text(r.read().decode("utf-8", "ignore"))


# --- Cloud STT #2: ElevenLabs Scribe (preferred when configured) ----------------------
# Scribe is NOT OpenAI-compatible, so it can't be reached by pointing VOICE_STT_URL at it:
# the auth header is xi-api-key (not Bearer), the model field is model_id, and the reply is
# JSON with the transcript under "text". Opt-in via VOICE_STT_11LABS_KEY, or
# VOICE_STT_11LABS_ENV_FILE -> a KEY=VALUE file holding ELEVENLABS_API_KEY. With no key this
# whole layer is inert and the ears behave exactly as before.
ELEVEN_STT_URL = os.environ.get("VOICE_STT_11LABS_URL",
                                "https://api.elevenlabs.io/v1/speech-to-text").strip()
ELEVEN_STT_MODEL = os.environ.get("VOICE_STT_11LABS_MODEL", "scribe_v2").strip()
# Scribe takes ISO-639-3 here (zho), unlike the OpenAI-style "zh" above.
ELEVEN_STT_LANG = os.environ.get("VOICE_STT_11LABS_LANG", "zho").strip()


def _eleven_key() -> str:
    key = os.environ.get("VOICE_STT_11LABS_KEY", "").strip()
    path = os.environ.get("VOICE_STT_11LABS_ENV_FILE", "").strip()
    if not key and path:
        try:
            with open(os.path.expanduser(path)) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("export "):
                        line = line[7:]
                    if line.startswith(("VOICE_STT_11LABS_KEY=", "ELEVENLABS_API_KEY=")):
                        key = line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError as e:
            _dlog(f"11labs STT env file unreadable ({type(e).__name__})")
    return key


_ELEVEN_KEY = _eleven_key()
USE_ELEVEN = bool(_ELEVEN_KEY)
# Either cloud ear means one transcription per turn: skip the in-turn streaming polls and
# let near-end energy drive the endpoint (same reasoning as the Groq note above).
USE_CLOUD_STT = USE_GROQ or USE_ELEVEN


def _eleven_transcribe(wav: str) -> str:
    with open(wav, "rb") as f:
        audio = f.read()
    b = "----rbkelevenboundary"
    fields = {"model_id": ELEVEN_STT_MODEL,
              "tag_audio_events": "false",     # no [laughter]-style annotations
              "diarize": "false",              # single speaker on a phone leg
              "timestamps_granularity": "none"}
    if ELEVEN_STT_LANG:
        fields["language_code"] = ELEVEN_STT_LANG
    body = b"".join(
        (f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n').encode()
        for k, v in fields.items())
    body += (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\n'
             f'Content-Type: audio/wav\r\n\r\n').encode() + audio + f'\r\n--{b}--\r\n'.encode()
    req = urllib.request.Request(
        ELEVEN_STT_URL, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={b}",
                 "xi-api-key": _ELEVEN_KEY,
                 "User-Agent": "ringback-voice"})
    timeout = float(os.environ.get("VOICE_STT_11LABS_TIMEOUT", "8") or "8")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", "ignore"))
    return _clean_text(data.get("text", ""))


def _transcribe_final(wav: str) -> str:
    """End-of-turn transcript: Scribe, then Groq, then the whisper-server/cli path.

    Both fallbacks stay: if the ears go silent mid-call the caller is talking into a void.
    """
    if USE_ELEVEN:
        try:
            return _eleven_transcribe(wav)
        except Exception as e:
            _dlog(f"11labs STT failed ({type(e).__name__}: {str(e)[:120]}) -> groq/local")
    if USE_GROQ:
        try:
            return _groq_transcribe(wav)
        except Exception as e:
            _dlog(f"cloud STT failed ({type(e).__name__}: {str(e)[:120]}) -> local whisper")
    return _transcribe_stream(wav)


# --- Cloud STT #2b: ElevenLabs Scribe v2 Realtime (streaming, opt-in) -----------------
# Pushes the growing recording to a WebSocket WHILE the caller talks, so when our own energy-based
# endpoint fires we only send commit=true and wait for the committed transcript (~150ms
# median per the docs) instead of uploading + transcribing the whole clip. Our turn logic
# stays in charge (commit_strategy=manual). Any failure / timeout / empty result returns ""
# and the caller falls back to the batch chain above. Off unless VOICE_STT_11LABS_STREAM=1
# and websocket-client is importable; rollback = set it to 0 and reconnect.
ELEVEN_RT_URL = os.environ.get("VOICE_STT_11LABS_RT_URL",
                               "wss://api.elevenlabs.io/v1/speech-to-text/realtime").strip()
ELEVEN_RT_MODEL = os.environ.get("VOICE_STT_11LABS_RT_MODEL", "scribe_v2_realtime").strip()
ELEVEN_RT_COMMIT_WAIT = float(os.environ.get("VOICE_STT_11LABS_RT_WAIT", "3") or "3")
_RT_RATES = (8000, 16000, 22050, 24000, 44100, 48000)
try:
    import websocket as _ws_mod      # websocket-client
except Exception:                    # not installed -> streaming stays off
    _ws_mod = None
USE_ELEVEN_RT = (USE_ELEVEN and _ws_mod is not None
                 and os.environ.get("VOICE_STT_11LABS_STREAM", "0").strip() == "1")


def _pj_register_this_thread(name: str) -> None:
    """Our helper threads never call pjsua2 directly, but Python's cyclic GC can run on ANY
    thread that allocates, and collecting a pjsua2 object (old Call etc.) from an
    unregistered thread trips a pjlib assertion and aborts the whole process (11-03: the
    first realtime build died silently right after the first turn). Register defensively."""
    try:
        pj.Endpoint.instance().libRegisterThread(name)
    except Exception:
        pass


class _ElevenStream:
    """One realtime session per listen turn, fed from the recorder's growing WAV file.

    ONE worker thread owns the socket for its whole life (connect, send, recv, close).
    11-03: the first build sent from one thread, read from another and closed from a third;
    concurrent use of one TLS connection killed the process after the turn. finish()/abort()
    only set flags and wait for the worker."""

    def __init__(self, wav_path: str):
        self.path = wav_path
        self.off = None            # byte offset of PCM start, found once the header exists
        self.rate = 16000
        self.committed: list[str] = []
        self.err = ""
        self.sent_bytes = 0
        self.want_commit = threading.Event()
        self.stop = threading.Event()
        self.done = threading.Event()
        self.result = ""
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _read_new(self) -> bytes:
        try:
            with open(self.path, "rb") as f:
                if self.off is None:
                    head = f.read(256)
                    if len(head) < 44 or head[:4] != b"RIFF":
                        return b""
                    at = head.find(b"fmt ", 12)
                    if at >= 0 and at + 16 <= len(head):
                        r = struct.unpack_from("<I", head, at + 12)[0]
                        if r in _RT_RATES:
                            self.rate = r
                    at = head.find(b"data", 12)
                    if at < 0:
                        return b""
                    self.off = at + 8
                f.seek(self.off + self.sent_bytes)
                buf = f.read()
        except OSError:
            return b""
        return buf[:len(buf) - (len(buf) % 2)]

    def _send(self, ws, pcm: bytes, commit: bool = False):
        ws.send(json.dumps({"message_type": "input_audio_chunk",
                            "audio_base_64": base64.b64encode(pcm).decode("ascii"),
                            "commit": commit, "sample_rate": self.rate}))
        self.sent_bytes += len(pcm)

    def _drain(self, ws, wait: float) -> bool:
        """Read whatever has arrived within `wait` seconds. True once a commit came back."""
        import select
        got = False
        end = time.time() + wait
        while True:
            left = max(0.0, end - time.time())
            sock = ws.sock
            pending = getattr(sock, "pending", lambda: 0)()
            if not pending:
                r, _, _ = select.select([sock], [], [], left)
                if not r:
                    return got
            raw = ws.recv()
            if not raw:
                raise ConnectionError("closed by server")
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            t = m.get("message_type", "")
            if t == "committed_transcript":
                self.committed.append(m.get("text", "") or "")
                got = True
                return got
            if "error" in t or t in ("quota_exceeded", "rate_limited"):
                raise RuntimeError(t + ": " + str(m.get("message") or m.get("error") or "")[:80])

    def _run(self):
        _pj_register_this_thread("stt-rt")
        ws = None
        try:
            t0 = time.time()
            while self.off is None and not self.stop.is_set() and time.time() - t0 < 3:
                self._read_new()
                time.sleep(0.05)
            if self.stop.is_set():
                return
            q = {"model_id": ELEVEN_RT_MODEL, "audio_format": f"pcm_{self.rate}",
                 "commit_strategy": "manual"}
            if ELEVEN_STT_LANG:
                q["language_code"] = ELEVEN_STT_LANG
            ws = _ws_mod.create_connection(ELEVEN_RT_URL + "?" + urllib.parse.urlencode(q),
                                           header=[f"xi-api-key: {_ELEVEN_KEY}"], timeout=5)
            while not self.stop.is_set() and not self.want_commit.is_set():
                pcm = self._read_new()
                if pcm:
                    self._send(ws, pcm)
                self._drain(ws, 0.1)          # partials / session_started; keeps buffers empty
            if self.want_commit.is_set() and not self.stop.is_set():
                tail = self._read_new() or b"\x00\x00" * (self.rate // 50)   # 20ms silence
                self._send(ws, tail, commit=True)
                end = time.time() + ELEVEN_RT_COMMIT_WAIT
                while time.time() < end and not self._drain(ws, end - time.time()):
                    pass
                self.result = "".join(self.committed).strip()
        except Exception as e:
            self.err = f"{type(e).__name__}: {str(e)[:60]}"
        finally:
            if ws is not None:
                try:
                    ws.close(timeout=1)
                except Exception:
                    pass
            self.done.set()

    def finish(self) -> str:
        """Commit the tail, wait for the transcript. "" on failure/timeout (caller falls back)."""
        self.want_commit.set()
        if not self.done.wait(ELEVEN_RT_COMMIT_WAIT + 2.5):
            self.stop.set()
            self.err = self.err or "timeout"
        if self.err:
            _dlog(f"11labs realtime STT: {self.err} -> batch")
        return _clean_text(self.result) if self.result and not self.err else ""

    def abort(self):
        self.stop.set()


def _words(t: str) -> int:
    """Progress measure for the streamed transcript; CJK has no spaces, so count chars."""
    return len(t) if any("\u4e00" <= c <= "\u9fff" for c in t) else len(t.split())


# Item 7 (11-03): VOICE_STT_MODE = text (default, unchanged) | audio | both.
# audio: skip STT entirely and hand the caller's turn's raw audio back to the model (for natively
# multimodal hosts like Gemini/GPT); both: transcript AND audio. The audio lives only in
# memory (CallSession.last_audio) until the MCP tool result picks it up; capped per turn.
STT_MODE = os.environ.get("VOICE_STT_MODE", "text").strip().lower()
if STT_MODE not in ("text", "audio", "both"):
    STT_MODE = "text"
AUDIO_MAX_SEC = float(os.environ.get("VOICE_STT_AUDIO_MAX_SEC", "60") or "60")
AUDIO_ONLY_PLACEHOLDER = "[the caller spoke; the audio is attached]"


def _wav_bytes_capped(path: str, max_sec: float = AUDIO_MAX_SEC) -> bytes:
    """Read a WAV and return it as in-memory WAV bytes, truncated to the first max_sec."""
    try:
        with wave.open(path, "rb") as r:
            params = r.getparams()
            n = min(r.getnframes(), int(params.framerate * max_sec))
            frames = r.readframes(n)
    except Exception:
        return b""
    if not frames:
        return b""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(params.nchannels)
        w.setsampwidth(params.sampwidth)
        w.setframerate(params.framerate)
        w.writeframes(frames)
    return buf.getvalue()


def _capture_turn(snapshot_fn, is_disconnected, max_sec: float = LISTEN_MAX_SEC,
                  start_timeout: float = START_TIMEOUT, end_silence: float = END_SILENCE,
                  energy_fn=None, end_rms: float = LISTEN_END_RMS,
                  stats: dict | None = None, transcribe: bool = True) -> str:
    """The streaming capture turn-loop, with NO pjsua2/SIP dependency so it can be
    unit-tested with a file-fed audio source (see tests/test_capture.py).

      snapshot_fn()    -> valid-header WAV path of audio captured so far, or "".
      energy_fn()      -> tail RMS of the near-end (the robust "still talking?" signal).
      is_disconnected()-> True if the call dropped (bail at once; returns None).
      stats            -> optional dict, filled with the turn's diagnostics (exit reason,
                          rms_max vs threshold, voiced/total polls, ...) so the caller can
                          log ONE line that says WHY a turn came back silent.

    The user counts as VOICED on a given poll if the near-end energy is above `end_rms` OR
    a new word appeared. End-of-turn is then:
      - BEFORE any voice: wait up to `start_timeout` (a thinking pause must not end it) -> "",
      - AFTER voice: end `end_silence` after voice stops (energy fell silent AND no new words).
    Energy — not transcript text — drives the endpoint, because whisper re-words the same
    noisy clip every poll (held the turn open ~15s) and word-count plateaus chop real speech.
    Returns the streamed transcript; the final re-transcribe is the caller's job.
    """
    start = time.time()
    text = ""
    max_words = 0
    last_voice = None         # elapsed time we last saw speech energy or a new word
    last_check = 0.0
    polls = voiced_polls = snaps = 0
    rms_max = 0.0
    exit_reason = "max_sec"

    def _fill():
        if stats is not None:
            stats.update(exit=exit_reason, dur=time.time() - start, rms_max=rms_max,
                         thresh=end_rms, polls=polls, voiced_polls=voiced_polls,
                         snaps=snaps, words=max_words)

    while time.time() - start < max_sec:
        time.sleep(0.1)
        if is_disconnected():
            exit_reason = "hangup"
            _fill()
            return None
        el = time.time() - start
        polls += 1
        rms = energy_fn() if energy_fn is not None else 0.0           # checked every poll
        rms_max = max(rms_max, rms)
        voiced = energy_fn is not None and rms > end_rms
        if transcribe and not USE_CLOUD_STT and el >= 0.5 and (el - last_check) >= 0.3:   # transcribe ~3x/sec
            last_check = el
            snap = snapshot_fn()
            t = _transcribe_stream(snap) if snap else ""   # already hallucination-filtered
            if snap:
                snaps += 1
                _rm(snap)
            if t:
                text = t
                wc = _words(t)
                # a lone backchannel is only "voiced" if the energy says so (whisper may
                # have invented it on silence) -- otherwise it would flip the turn early
                if wc > max_words and not _is_backchannel(t):
                    max_words = wc
                    voiced = True
        if voiced:
            voiced_polls += 1
            last_voice = el
        if last_voice is None:
            if el >= start_timeout:
                exit_reason = "start_timeout"
                break                         # never started speaking -> ""
        elif (el - last_voice) >= end_silence:
            exit_reason = "end_silence"
            break                             # voice stopped for end_silence -> end turn
    _fill()
    return text


def _listen_summary(st: dict, wav_bytes: int, result: str, hangup: bool = False) -> None:
    """ONE attributable stderr line per listen turn. The fields disambiguate every cause of
    a silent turn: wav=44B -> no RTP audio ever arrived; rms_max < thresh -> audio arrived
    but stayed under the speech threshold; voiced>0 with text=0w -> speech energy was seen
    but whisper produced no usable words (see also the VOICE_DEBUG filter logs)."""
    outcome = ("[CALL ENDED]" if (hangup or st.get("exit") == "hangup")
               else ("ok" if result else "[SILENCE]"))
    _dlog("listen: exit=%s dur=%.1fs rms_max=%.0f thresh=%.0f voiced=%d/%d snaps=%d "
          "wav=%dB text=%dw stt=%.2fs(%s) -> %s"
          % (st.get("exit", "?"), st.get("dur", 0.0), st.get("rms_max", 0.0),
             st.get("thresh", 0.0), st.get("voiced_polls", 0), st.get("polls", 0),
             st.get("snaps", 0), wav_bytes, len(result.split()), st.get("stt", 0.0),
             st.get("src", "batch"), outcome))


class _BargeState:
    """Decide if/when the user barged in, from a stream of near-end RMS samples while we
    speak. Pure + tiny so the REAL harvested audio (tests/test_barge.py) validates the same
    decision the live call makes. feed(t, rms) -> 'barge' (cut in -> stop talking), 'echo'
    (suspiciously early sustained energy -> treat as device echo, go half-duplex), or None.
    """

    def __init__(self, thresh: float, debounce: int = BARGE_DEBOUNCE,
                 early_sec: float = EARLY_BARGE_SEC):
        self.thresh = thresh
        self.debounce = debounce
        self.early_sec = early_sec
        self.over = 0
        self.rms_max = 0.0
        self.over_max = 0

    def feed(self, t: float, rms: float):
        self.rms_max = max(self.rms_max, rms)
        if rms > self.thresh:
            self.over += 1
            self.over_max = max(self.over_max, self.over)
            if self.over >= self.debounce:        # sustained, not a transient
                self.over = 0
                return "echo" if t < self.early_sec else "barge"
        else:
            self.over = 0
        return None


def _tail_rms(path: str, seconds: float = 0.25) -> float:
    """RMS of the last `seconds` of a (possibly still-growing) 16-bit mono WAV."""
    try:
        size = os.path.getsize(path)
        if size <= 44:
            return 0.0
        nbytes = int(16000 * 2 * seconds)
        with open(path, "rb") as f:
            f.seek(max(44, size - nbytes))
            raw = f.read()
        if len(raw) < 2:
            return 0.0
        n = len(raw) // 2
        vals = struct.unpack("<%dh" % n, raw[: n * 2])
        return math.sqrt(sum(v * v for v in vals) / n)
    except OSError:
        return 0.0


def _wav_pcm(path: str):
    """(rate, int16 PCM bytes) of a pjsua recording; header sizes may be stale, so read
    the fmt chunk for the rate and take everything after 'data'."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return 16000, b""
    if len(data) <= 44 or data[:4] != b"RIFF":
        return 16000, b""
    rate = 16000
    at = data.find(b"fmt ", 12)
    if at >= 0 and at + 16 <= len(data):
        rate = struct.unpack_from("<I", data, at + 12)[0] or 16000
    at = data.find(b"data", 12)
    if at < 0:
        return rate, b""
    pcm = data[at + 8:]
    return rate, pcm[:len(pcm) - (len(pcm) % 2)]


def _write_wav(path: str, rate: int, pcm: bytes) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def _loud_seconds(pcm: bytes, rate: int, thresh: float, win_sec: float = 0.1) -> float:
    """Seconds of 0.1 s windows whose RMS clears `thresh` (pure stdlib)."""
    n = max(int(rate * win_sec), 1) * 2
    loud = 0
    for i in range(0, len(pcm) - n + 1, n):
        seg = pcm[i:i + n]
        cnt = len(seg) // 2
        vals = struct.unpack("<%dh" % cnt, seg)
        if math.sqrt(sum(v * v for v in vals) / cnt) > thresh:
            loud += 1
    return loud * win_sec


def _rms_windows(pcm: bytes, rate: int, win_sec: float = 0.1):
    """RMS of each win_sec window (same algorithm as _loud_seconds, pure stdlib).

    Diagnostics only: lets the quiet: log line say how loud the caller actually was, so a
    voiced=0 round can be told apart as 'suppressed to nothing' vs 'just quiet'."""
    n = max(int(rate * win_sec), 1) * 2
    out = []
    for i in range(0, len(pcm) - n + 1, n):
        seg = pcm[i:i + n]
        cnt = len(seg) // 2
        vals = struct.unpack("<%dh" % cnt, seg)
        out.append(math.sqrt(sum(v * v for v in vals) / cnt))
    return out


def _wav_end(path: str) -> int:
    """How many PCM bytes are already on disk, i.e. "start listening from now".

    A fresh wait/sentry must not replay what is already in the roll: offset 0 would hand it
    the caller's voice from a minute ago (11-05: wait_for_voice reported the same rms=3456 twice on a
    real call because every call started reading from byte 0). Returns 0 when the header is
    not readable yet, which is also the right answer for a just-created file."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
            size = os.path.getsize(path)
        if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            return 0
        pos = 12
        while pos + 8 <= len(head):
            cid = head[pos:pos + 4]
            csz = struct.unpack("<I", head[pos + 4:pos + 8])[0]
            body = pos + 8
            if cid == b"data":
                n = max(0, size - body)
                return n - (n % 2)
            pos = body + csz + (csz & 1)
    except Exception:
        pass
    return 0


def _wav_tail(path: str, offset: int):
    """PCM that has landed on disk past `offset` in a wav file that may STILL be being written.

    pjsua's recorder only fixes the RIFF/data length fields when the file is finalized, so
    wave.open() reports an empty or short file mid-recording. We parse the header ourselves and
    read to EOF. `offset` counts bytes from the first sample. Returns (rate, pcm, new_offset);
    (0, b"", offset) when the file is not a usable 16-bit mono wav yet.

    Read-only on purpose: the sentry must never consume the roll, because quiet_listen's
    _bg_swap() is the one allowed to clear it (design 3d, solution 1)."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
            if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
                return 0, b"", offset
            rate, bits, chans, pos, data_at = 0, 0, 0, 12, -1
            while pos + 8 <= len(head):
                cid = head[pos:pos + 4]
                csz = struct.unpack("<I", head[pos + 4:pos + 8])[0]
                body = pos + 8
                if cid == b"fmt " and body + 16 <= len(head):
                    chans = struct.unpack("<H", head[body + 2:body + 4])[0]
                    rate = struct.unpack("<I", head[body + 4:body + 8])[0]
                    bits = struct.unpack("<H", head[body + 14:body + 16])[0]
                elif cid == b"data":
                    data_at = body
                    break
                pos = body + csz + (csz & 1)
            if data_at < 0 or rate <= 0 or bits != 16 or chans != 1:
                return 0, b"", offset
            f.seek(data_at + max(0, offset))
            pcm = f.read()
    except Exception:
        return 0, b"", offset
    n = len(pcm) - (len(pcm) % 2)
    return rate, pcm[:n], max(0, offset) + n


def _quiet_report(chunks, chunk_sec: float, thresh: float, transcribe, hear, stats: dict) -> str:
    """Turn background audio into lines 'm:ss transcript[heard: ...]', one per voiced chunk.

    chunks: list of (rate, pcm) pieces of chunk_sec each. Too-quiet chunks are never sent to
    STT (whisper invents sentences from silence). Pure: transcribe/hear are passed in, so the
    tests can feed fake audio without SIP or network."""
    from concurrent.futures import ThreadPoolExecutor
    voiced = [(i, rate, pcm) for i, (rate, pcm) in enumerate(chunks)
              if _loud_seconds(pcm, rate, thresh) >= QUIET_VOICED_SEC]
    tops = [max(_rms_windows(pcm, rate) or [0.0]) for rate, pcm in chunks]
    stats.update(chunks=len(chunks), voiced=len(voiced),
                 rms_max=(max(tops) if tops else 0.0), rms_tops=tops)

    def one(item):
        i, rate, pcm = item
        wav = tempfile.mktemp(prefix="quiet-", suffix=".wav")
        try:
            _write_wav(wav, rate, pcm)
            try:
                text = _clean_text(transcribe(wav) or "")
            except Exception:
                text = ""
            return i, (text + hear(wav, text)).strip()
        finally:
            _rm(wav)

    with ThreadPoolExecutor(max_workers=4) as ex:
        results = sorted(ex.map(one, voiced))
    lines = []
    for i, line in results:
        if line:
            t = int(i * chunk_sec)
            lines.append("%d:%02d %s" % (t // 60, t % 60, line))
    stats.update(lines=len(lines))
    return "\n".join(lines)


class _Call(pj.Call):
    def __init__(self, acc, session):
        super().__init__(acc)
        self.session = session

    def onCallState(self, prm):
        ci = self.getInfo()
        self.session.last_state = ci.state
        if ci.state == pj.PJSIP_INV_STATE_CONFIRMED:
            self.session.connected = True
        if ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            self.session.connected = False
            self.session.disconnected = True

    def onCallMediaState(self, prm):
        ci = self.getInfo()
        for i, mi in enumerate(ci.media):
            if (mi.type == pj.PJMEDIA_TYPE_AUDIO
                    and mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE):
                self.session.aud = self.getAudioMedia(i)


class CallSession:
    last_audio = b""   # item 7: the caller's last turn as WAV bytes (VOICE_STT_MODE audio/both)
    def __init__(self):
        self.ep = None
        self.acc = None
        self.call = None
        self.aud = None
        self.connected = False
        self.disconnected = False
        self.last_state = None
        self.noise_floor = 0.0  # ambient RMS measured at call connect (see place_call)
        self.half_duplex = HALF_DUPLEX  # flips on if speaker echo is detected mid-call
        self.log = []          # unified conversation timeline (claude + user turns)
        self._bg_lock = threading.Lock()
        self._bg_rec = None      # background recorder (quiet listen), see _bg_start
        self._bg_path = ""
        self._bg_t0 = 0.0
        self._bg_pending = []    # finished segments not yet collected: [(path, seconds)]
        self._bg_busy = 0        # >0 while a normal talk/listen turn owns the audio
        self._push_stop = threading.Event()   # set to stop the push-on-voice sentry
        self._push_thread = None

    def _pump(self, seconds: float):
        # pjsua2 runs its own worker thread (threadCnt=1) that processes RTP and
        # events continuously, so we just wait — no manual event pumping needed.
        time.sleep(seconds)

    def _reg(self):
        # any thread calling into pjsua2 must be registered (MCP tool calls may
        # land on different threadpool threads).
        try:
            self.ep.libRegisterThread("mcp")
        except Exception:
            pass

    def _measure_rms(self, duration: float = 0.6) -> float:
        """Sample ambient audio (right after answer, before anyone speaks) to learn
        the background noise floor, so thresholds can be raised to clear it."""
        self._reg()
        if not (self.connected and self.aud):
            return 0.0
        wav = tempfile.mktemp(suffix=".wav")
        try:
            rec = pj.AudioMediaRecorder()
            rec.createRecorder(wav)
            self.aud.startTransmit(rec)
            time.sleep(duration)
            try:
                self.aud.stopTransmit(rec)
            except Exception:
                pass
            del rec
            return _tail_rms(wav, seconds=duration)
        except Exception:
            return 0.0
        finally:
            _rm(wav)

    def start_lib(self):
        self.ep = pj.Endpoint()
        self.ep.libCreate()
        cfg = pj.EpConfig()
        cfg.uaConfig.threadCnt = 1          # worker thread keeps the call alive
        # quiet by default; raise VOICE_CONSOLE_LEVEL (e.g. 5) to see SIP signaling, or set
        # VOICE_LOG_FILE to capture full pjsua logs to a file for debugging
        cfg.logConfig.level = int(os.environ.get("VOICE_LOG_LEVEL", "1"))
        cfg.logConfig.consoleLevel = int(os.environ.get("VOICE_CONSOLE_LEVEL", "0"))
        # all pjsua output goes through the redacting writer (privacy.py); keep a ref on
        # self so the SWIG object outlives libInit
        self._logwriter = _RedactingLogWriter(
            os.environ.get("VOICE_LOG_FILE", ""),
            raw=os.environ.get("VOICE_LOG_RAW", "0").strip().lower() in ("1", "true", "yes"))
        cfg.logConfig.writer = self._logwriter
        # STUN: discover our PUBLIC RTP address so the SDP advertises something the remote
        # media server can actually return audio to. Without it, a client behind NAT (notably
        # inside Docker — bridge or Docker Desktop) advertises a private address and gets
        # ONE-WAY audio (it hears the user fine, the user's reply never arrives). Off unless
        # VOICE_STUN is set; the Docker plugin sets it (the local/native path doesn't need it).
        _stun = os.environ.get("VOICE_STUN", "").strip()
        if _stun:
            cfg.uaConfig.stunServer.append(_stun)
            _dlog(f"STUN enabled: {_stun}")
        self.ep.libInit(cfg)

        self.ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, pj.TransportConfig())
        tls = pj.TransportConfig()
        tls.tlsConfig.verifyServer = False
        tls.tlsConfig.method = pj.PJSIP_TLSV1_2_METHOD
        self.ep.transportCreate(pj.PJSIP_TRANSPORT_TLS, tls)
        self.ep.libStart()
        # We never use the local sound card — all media is file<->call(RTP). On headless
        # Linux / containers we force a NULL audio device so pjsua2 never needs a real one
        # (this is what lets the engine run fully headless). On macOS we KEEP the existing,
        # known-good behavior by default so nothing regresses; set VOICE_NULL_AUDIO=1 to
        # force null there too, or =0 to disable everywhere.
        _null = os.environ.get("VOICE_NULL_AUDIO", "auto").strip().lower()
        if _null in ("1", "true", "yes") or (_null == "auto" and not IS_MAC):
            try:
                self.ep.audDevManager().setNullDev()
            except Exception as e:
                # A headless-audio misconfig must not hide behind a green init — surface it
                # on stderr (stdout is the MCP transport). Non-fatal: pjsua may still cope.
                _dlog(f"setNullDev() failed: {e}")

        acfg = pj.AccountConfig()
        # add the caller-ID display name to the From header if configured
        acfg.idUri = f'"{SIP_DISPLAY_NAME}" <{SIP_ID}>' if SIP_DISPLAY_NAME else SIP_ID
        acfg.regConfig.registrarUri = ""    # do NOT register (avoids self-call fork)
        acfg.sipConfig.authCreds.append(
            pj.AuthCredInfo("digest", "*", SIP_USER, 0, SIP_PASS))
        acfg.sipConfig.proxies.append(SIP_PROXY)
        acfg.mediaConfig.srtpUse = pj.PJMEDIA_SRTP_MANDATORY
        acfg.mediaConfig.srtpSecureSignaling = 0
        # RTP media port base. Default (0) keeps pjsua's 4000; override (VOICE_RTP_PORT) when
        # a second instance must coexist with the running MCP server (which holds 4000/4002).
        rtp_port = int(os.environ.get("VOICE_RTP_PORT", "0"))
        if rtp_port:
            acfg.mediaConfig.transportConfig.port = rtp_port
        self.acc = pj.Account()
        self.acc.create(acfg)

    def place_call(self, answer_timeout: float = 25.0, callee: str | None = None) -> bool:
        # `callee` lets a caller dial a specific SIP address (the remote/multi-user server
        # passes each user's Linphone address); defaults to env SIP_CALLEE (local self-call),
        # so the local stdio MCP is unchanged.
        import gc
        self._reg()
        self.connected = False
        self.disconnected = False
        self.half_duplex = HALF_DUPLEX   # re-evaluate echo per call (unless forced on)
        self.echo_strikes = 0
        self.last_return = None
        self.aud = None
        if self.call is not None:        # clear any previous call object
            self.call = None
            gc.collect()
        self.call = _Call(self.acc, self)
        self.call.makeCall(callee or SIP_CALLEE, pj.CallOpParam(True))
        end = time.time() + answer_timeout
        while time.time() < end:
            time.sleep(0.1)
            if self.connected and self.aud is not None:
                time.sleep(0.4)   # let media settle
                self.noise_floor = self._measure_rms(0.6)   # calibrate to ambient noise
                _dlog("call connected: noise_floor=%.0f -> listen_thresh=%.0f "
                      "barge_thresh=%.0f end_rms=%.0f"
                      % (self.noise_floor,
                         _eff_threshold(LISTEN_RMS_BASE, self.noise_floor, LISTEN_NOISE_FACTOR),
                         _eff_threshold(BARGE_RMS_BASE, self.noise_floor), LISTEN_END_RMS))
                if not USE_CLOUD_STT:
                    _wsrv_warm()   # lazily start the whisper-server now so it's ready to capture
                self._bg_start()
                return True
            if self.disconnected:
                return False
        return False

    # ---- quiet listen: background recording between turns -------------------------------
    def _bg_swap(self):
        """Start a fresh recorder, then stop the old one (overlap, so nothing is lost).
        Returns (old_path, seconds) or None. Caller holds _bg_lock."""
        # 11-03: listen()/speak_interruptible() call _bg_enter() BEFORE their own _reg(), and each
        # MCP tool call runs on a fresh thread -> createRecorder() on an unregistered thread hit
        # the pjlib assertion and aborted the whole process (4 crashes, blamed on streaming STT).
        self._reg()
        old = None
        if self._bg_rec is not None:
            old = (self._bg_path, time.time() - self._bg_t0)
        new_rec = None
        if self.connected and self.aud is not None and not self.disconnected:
            path = tempfile.mktemp(prefix="bg-", suffix=".wav")
            try:
                new_rec = pj.AudioMediaRecorder()
                new_rec.createRecorder(path)
                self.aud.startTransmit(new_rec)
            except Exception as e:
                _dlog(f"quiet: recorder start failed ({type(e).__name__})")
                new_rec = None
        if self._bg_rec is not None:
            try:
                self.aud.stopTransmit(self._bg_rec)
            except Exception:
                pass
            self._bg_rec = None          # drop the SWIG object -> pjsua finalizes the file
        if new_rec is not None:
            self._bg_rec, self._bg_path, self._bg_t0 = new_rec, path, time.time()
        else:
            self._bg_path, self._bg_t0 = "", 0.0
        return old

    def _bg_start(self):
        """Call right after a call connects (place_call / inbound answer)."""
        if not QUIET_LISTEN:
            return
        self._reg()
        with self._bg_lock:
            self._bg_clear_locked()
            self._bg_busy = 0
            self._bg_swap()
        self._push_start()

    def _bg_clear_locked(self):
        if self._bg_rec is not None:
            try:
                self.aud.stopTransmit(self._bg_rec)
            except Exception:
                pass
            self._bg_rec = None
        for p, _ in self._bg_pending:
            _rm(p)
        self._bg_pending = []
        if self._bg_path:
            _rm(self._bg_path)
        self._bg_path, self._bg_t0 = "", 0.0

    def _bg_stop(self):
        self._push_stop.set()
        with self._bg_lock:
            self._bg_clear_locked()

    # ---- push on voice: tell the model the moment the caller makes a sound ----------------------
    def _push_start(self):
        """Start the sentry thread for this call (no-op unless VOICE_PUSH_ON_VOICE=1)."""
        if not (PUSH_ON_VOICE and QUIET_LISTEN):
            return
        if PUSH_SINK is None:
            _dlog("push: no sink set, sentry not started")
            return
        if self._push_thread is not None and self._push_thread.is_alive():
            return
        self._push_stop.clear()
        self._push_thread = threading.Thread(target=self._push_sentry, daemon=True)
        self._push_thread.start()
        _dlog("push: sentry started tick=%.1fs gap=%.1fs min_interval=%.0fs"
              % (PUSH_TICK, PUSH_GAP_SEC, PUSH_MIN_INTERVAL))

    def _push_emit(self, content: str, kind: str, **extra):
        sink = PUSH_SINK
        if sink is None:
            return
        meta = {"event": "voice", "caller": "user", "kind": kind}
        meta.update(extra)
        try:
            sink(content, meta)
        except Exception as e:
            _dlog(f"push: emit failed ({type(e).__name__})")

    def _push_describe(self, rate: int, pcm: bytes, transcribe: bool):
        """(text, labels) for a stretch. Transcription is the slow part, so the sentry only
        asks for it when a stretch has ended (short pieces make whisper invent words)."""
        wav = tempfile.mktemp(prefix="push-", suffix=".wav")
        try:
            _write_wav(wav, rate, pcm)
            text = ""
            if transcribe:
                try:
                    text = _clean_text(_transcribe_final(wav) or "")
                except Exception:
                    text = ""
            try:
                labels = sound_events.hear(wav, text)
            except Exception:
                labels = ""
            return text, labels
        finally:
            _rm(wav)

    def _push_sentry(self):
        """Tail the background recording; push as soon as the caller makes a sound, stay quiet otherwise.

        Reads the roll without consuming it (see _wav_tail), so quiet_listen keeps working
        exactly as before and remains the fallback if pushes break."""
        path, off = "", 0
        seg = b""
        seg_rate = 16000
        seg_tops = []
        seg_start = 0.0
        quiet_run = 0.0
        open_stretch = False
        last_push = 0.0
        while not self._push_stop.is_set() and not self.disconnected:
            time.sleep(PUSH_TICK)
            if self._push_stop.is_set() or self.disconnected:
                break
            with self._bg_lock:
                cur, busy = self._bg_path, self._bg_busy
            if busy:                      # a normal talk/listen turn owns the audio: stand down
                # the talk/listen turn owns this audio: resume from now, never replay it
                path, off = cur, (_wav_end(cur) if cur else 0)
                seg, seg_tops, open_stretch, quiet_run = b"", [], False, 0.0
                continue
            if not cur:
                continue
            if cur != path:               # _bg_swap rotated the file, or first look
                path, off = cur, (_wav_end(cur) if path == "" else 0)
            rate, pcm, off = _wav_tail(path, off)
            if not (rate and pcm):
                continue
            thresh = _eff_threshold(QUIET_RMS_BASE, self.noise_floor, LISTEN_NOISE_FACTOR)
            dur = len(pcm) / 2.0 / rate
            loud = _loud_seconds(pcm, rate, thresh)
            top = max(_rms_windows(pcm, rate) or [0.0])
            now = time.time()
            if loud >= QUIET_VOICED_SEC:
                seg_rate = rate
                seg += pcm
                seg_tops.append(top)
                quiet_run = 0.0
                if not open_stretch:      # FIRST sound: push at once, do not wait for words
                    open_stretch, seg_start, last_push = True, now, now
                    _, labels = self._push_describe(rate, pcm, transcribe=False)
                    self._push_emit("[%s sound] %s rms=%.0f"
                                    % (time.strftime("%H:%M:%S", time.localtime(now)),
                                       labels or "", top),
                                    "onset", rms=round(top))
                elif now - last_push >= PUSH_MIN_INTERVAL:
                    last_push = now
                    self._push_emit("[still going, %d s] rms=%.0f" % (round(now - seg_start), top),
                                    "ongoing", rms=round(top))
            else:
                quiet_run += dur
                if open_stretch and quiet_run >= PUSH_GAP_SEC:
                    sec = len(seg) / 2.0 / seg_rate
                    text, labels = self._push_describe(seg_rate, seg, transcribe=True)
                    _dlog("push: stretch %.1fs rms_max=%.0f tops=%s text=%d"
                          % (sec, max(seg_tops or [0.0]),
                             ",".join("%.0f" % v for v in seg_tops[:16]), len(text)))
                    self._push_emit("[the last stretch, %d s] %s%s rms_max=%.0f"
                                    % (round(sec), text, labels, max(seg_tops or [0.0])),
                                    "stretch", seconds=round(sec),
                                    rms_max=round(max(seg_tops or [0.0])))
                    seg, seg_tops, open_stretch, quiet_run = b"", [], False, 0.0
        _dlog("push: sentry stopped")

    def _bg_enter(self):
        """A normal turn starts: keep what was recorded before it."""
        if not QUIET_LISTEN:
            return
        with self._bg_lock:
            self._bg_busy += 1
            if self._bg_busy == 1:
                old = self._bg_swap()
                if old:
                    self._bg_pending.append(old)

    def _bg_leave(self):
        """The turn ended: its audio was already heard by listen(), so drop that segment."""
        if not QUIET_LISTEN:
            return
        with self._bg_lock:
            self._bg_busy = max(self._bg_busy - 1, 0)
            if self._bg_busy == 0:
                old = self._bg_swap()
                if old:
                    _rm(old[0])

    def wait_for_voice(self, max_sec: float = 10.0) -> str:
        """Block until the caller makes a sound, then return AT ONCE. '' / [SILENCE] on timeout.

        The push sentry still has to interrupt a model that has handed the turn back; waiting
        inside a tool is simpler, costs nothing while idle, and the sound itself ends the wait.
        Unlike quiet_listen (which waits for `seconds` of audio to pile up and then CONSUMES the
        roll), this only tails the roll read-only, so quiet_listen can still fetch the full audio
        afterwards if the words are wanted. No transcription here: being immediate is the point."""
        self._reg()
        if not QUIET_LISTEN:
            return "[QUIET LISTEN OFF]"
        if not (self.connected and self.aud) or self.disconnected:
            return ""
        max_sec = max(1.0, min(float(max_sec or 10.0), 600.0))
        if self._bg_rec is None and not self._bg_pending:
            self._bg_start()
        thresh = _eff_threshold(QUIET_RMS_BASE, self.noise_floor, LISTEN_NOISE_FACTOR)
        t0 = time.time()
        path, off = "", 0
        primed = False
        best = 0.0
        while time.time() - t0 < max_sec and not self.disconnected:
            time.sleep(WAIT_VOICE_TICK)
            with self._bg_lock:
                cur, busy = self._bg_path, self._bg_busy
            if busy or not cur:          # a talk/listen turn owns the audio: stand down
                path, off = cur, (_wav_end(cur) if cur else 0)
                primed = True
                continue
            if cur != path:              # first look, or _bg_swap rotated the file
                # first look: start from now. a rotation: the new file starts empty anyway.
                path, off = cur, (_wav_end(cur) if not primed else 0)
                primed = True
            rate, pcm, off = _wav_tail(path, off)
            if not (rate and pcm):
                continue
            if _loud_seconds(pcm, rate, thresh) < WAIT_VOICE_SEC:
                continue
            top = max(_rms_windows(pcm, rate) or [0.0])
            best = max(best, top)
            labels = ""
            wav = tempfile.mktemp(prefix="wait-", suffix=".wav")
            try:
                _write_wav(wav, rate, pcm)
                try:
                    labels = sound_events.hear(wav, "")
                except Exception:
                    labels = ""
            finally:
                _rm(wav)
            waited = time.time() - t0
            _dlog("wait_voice: hit after %.1fs rms=%.0f thresh=%.0f" % (waited, top, thresh))
            return "[waited %d s, sound] %s rms=%.0f" % (round(waited), labels, top)
        _dlog("wait_voice: timeout %.0fs thresh=%.0f peak=%.0f"
              % (max_sec, thresh, best))
        if self.disconnected:
            return ""
        return "[waited quietly %d s] [SILENCE]" % round(time.time() - t0)

    def quiet_listen(self, seconds: float = 8.0) -> str:
        """Don't speak; return what the caller did since the last collection (words + sound labels),
        waiting until at least `seconds` of audio has built up. '' if the caller was quiet."""
        self._reg()
        if not QUIET_LISTEN:
            return "[QUIET LISTEN OFF]"
        if not (self.connected and self.aud) or self.disconnected:
            return ""
        seconds = max(1.0, min(float(seconds or 8.0), 120.0))

        def buffered():
            with self._bg_lock:
                cur = (time.time() - self._bg_t0) if self._bg_rec is not None else 0.0
                return sum(d for _, d in self._bg_pending) + cur

        if self._bg_rec is None and not self._bg_pending:   # e.g. engine reloaded mid-call
            self._bg_start()
        while buffered() < seconds and not self.disconnected:
            time.sleep(0.1)
        with self._bg_lock:
            old = self._bg_swap()
            segs = self._bg_pending + ([old] if old else [])
            self._bg_pending = []
        rate, pcm = 16000, b""
        for p, _ in segs:
            r, b = _wav_pcm(p)
            if b:
                rate, pcm = r, pcm + b
            _rm(p)
        st = {}
        dur = len(pcm) / 2.0 / rate if rate else 0.0
        keep = int(QUIET_MAX_SEC * rate) * 2
        dropped = 0.0
        if len(pcm) > keep:
            dropped = (len(pcm) - keep) / 2.0 / rate
            pcm = pcm[-keep:]
        step = int(QUIET_CHUNK_SEC * rate) * 2
        chunks = [(rate, pcm[i:i + step]) for i in range(0, len(pcm), step)
                  if len(pcm[i:i + step]) >= rate * 2 // 2]      # skip a tail under 0.5 s
        thresh = _eff_threshold(QUIET_RMS_BASE, self.noise_floor, LISTEN_NOISE_FACTOR)
        _ts = time.time()
        body = _quiet_report(chunks, QUIET_CHUNK_SEC, thresh, _transcribe_final,
                             sound_events.hear, st)
        _dlog("quiet: dur=%.1fs dropped=%.0fs chunks=%d voiced=%d lines=%d thresh=%.0f "
              "rms_max=%.0f tops=%s took=%.2fs"
              % (dur, dropped, st.get("chunks", 0), st.get("voiced", 0),
                 st.get("lines", 0), thresh, st.get("rms_max", 0.0),
                 ",".join("%.0f" % v for v in (st.get("rms_tops") or [])[:16]),
                 time.time() - _ts))
        head = "[listened quietly %d s%s]" % (round(dur), ", kept only the last %d min" % (QUIET_MAX_SEC // 60)
                                     if dropped else "")
        if self.disconnected and not body:
            return ""
        return head + ("\n" + body if body else " [SILENCE]")

    def speak(self, text: str):
        self._reg()
        if not (self.connected and self.aud):
            return "not connected"
        wav = _tts_to_wav(text)
        dur = _wav_duration(wav)
        player = pj.AudioMediaPlayer()
        player.createPlayer(wav, pj.PJMEDIA_FILE_NO_LOOP)
        player.startTransmit(self.aud)
        end = time.time() + dur + 0.3
        while time.time() < end:        # poll so a hang-up stops us AT ONCE, not after the whole line
            time.sleep(0.05)
            if self.disconnected:
                break
        try:
            player.stopTransmit(self.aud)
        except Exception:
            pass
        del player
        _rm(wav)
        return "ended" if self.disconnected else "spoke"

    def listen(self, max_sec: float = LISTEN_MAX_SEC, end_silence: float = END_SILENCE,
               start_timeout: float = START_TIMEOUT) -> str:
        self._bg_enter()
        try:
            return self._listen(max_sec, end_silence, start_timeout)
        finally:
            self._bg_leave()

    def _listen(self, max_sec: float = LISTEN_MAX_SEC, end_silence: float = END_SILENCE,
                start_timeout: float = START_TIMEOUT) -> str:
        """Capture one user turn, STREAMED through the persistent whisper-server. Records
        and transcribes the growing clip ~3x/sec; the turn-end logic lives in the pure,
        file-testable _capture_turn (two-phase: wait `start_timeout` for the first word,
        then end `end_silence` after the last). No "your turn" beep — they just hear us
        finish and reply, like a real call. Returns the user's words, or "" on silence /
        hang-up (caller surfaces [SILENCE] / [CALL ENDED])."""
        self._reg()
        if not (self.connected and self.aud):
            _dlog("listen: no active media (connected=%s, disconnected=%s) -> [SILENCE]"
                  % (self.connected, self.disconnected))
            return ""
        self.last_audio = b""                  # item 7: only this turn's audio, never stale
        want_text = STT_MODE != "audio"
        rec_wav = tempfile.mktemp(suffix=".wav")
        rec = pj.AudioMediaRecorder()
        rec.createRecorder(rec_wav)
        self.aud.startTransmit(rec)
        # stream-capture via the pure, file-fed turn-loop (same code the tests exercise);
        # energy_fn = near-end tail RMS is the robust "still talking?" signal for end-of-turn
        st: dict = {}
        rt = None
        if USE_ELEVEN_RT and want_text:
            try:
                rt = _ElevenStream(rec_wav)
            except Exception as e:
                _dlog(f"11labs realtime STT: start failed ({type(e).__name__}) -> batch")
        text = _capture_turn(lambda: _wav_snapshot(rec_wav), lambda: self.disconnected,
                             max_sec=max_sec, start_timeout=start_timeout, end_silence=end_silence,
                             energy_fn=lambda: _tail_rms(rec_wav, 0.3), stats=st,
                             transcribe=want_text)
        try:
            self.aud.stopTransmit(rec)
        except Exception:
            pass
        del rec
        try:
            wav_bytes = os.path.getsize(rec_wav)   # 44B = header only -> NO RTP ever arrived
        except OSError:
            wav_bytes = 0
        if text is None or self.disconnected:    # hang-up mid-turn
            if rt is not None:
                rt.abort()
            _listen_summary(st, wav_bytes, "", hangup=True)
            _rm(rec_wav)
            return ""
        # never crossed the speech threshold -> silence; don't let STT invent words from it
        # (10-26: Linphone backgrounded, rms 0, whisper still returned whole sentences)
        quiet = st.get("exit") == "start_timeout" and not st.get("voiced_polls")
        snap = None if quiet else _wav_snapshot(rec_wav)   # full audio with a correct header
        _ts = time.time()
        final = ""
        if rt is not None:
            if snap:
                final = rt.finish()
                st["src"] = "rt" if final else "rt-miss"
            else:
                rt.abort()
        if snap and STT_MODE != "text":
            self.last_audio = _wav_bytes_capped(snap)
        if snap and not final and want_text:
            final = _transcribe_final(snap)
        st["stt"] = time.time() - _ts
        if snap:
            _rm(snap)
        if not want_text:
            text = AUDIO_ONLY_PLACEHOLDER if (not quiet and self.last_audio) else ""
        result = "" if quiet else (final or text)
        if result and _is_backchannel(result) and not st.get("voiced_polls"):
            _vlog(f"backchannel without speech energy -> treated as silence: {result!r}")
            result = ""
        result = (result + sound_events.hear(rec_wav, result)).strip()
        _listen_summary(st, wav_bytes, result)
        if not result and DEBUG_KEEP_WAV:        # keep the audio of a silent turn for replay
            try:
                os.makedirs(DEBUG_WAV_DIR, exist_ok=True)
                kept = os.path.join(DEBUG_WAV_DIR, "turn-%d.wav" % int(time.time()))
                os.replace(rec_wav, kept)
                _dlog(f"listen: silent-turn audio kept at {kept}")
            except OSError:
                _rm(rec_wav)
        else:
            _rm(rec_wav)
        return result

    def speak_interruptible(self, text: str, listen_after: bool = True,
                            silence_sec: float = 1.0, max_wait: float = LISTEN_MAX_SEC,
                            barge_rms: float = BARGE_RMS_BASE) -> dict:
        self._bg_enter()
        try:
            return self._speak_interruptible(text, listen_after, silence_sec, max_wait, barge_rms)
        finally:
            self._bg_leave()

    def _speak_interruptible(self, text: str, listen_after: bool = True,
                             silence_sec: float = 1.0, max_wait: float = LISTEN_MAX_SEC,
                             barge_rms: float = BARGE_RMS_BASE) -> dict:
        """Speak `text` while monitoring for the user talking over us (barge-in).

        If the user starts talking, we stop speaking immediately, note how far we
        got, and capture what they said. If we finish uninterrupted, we then
        listen for their reply (when listen_after). Returns a dict describing the
        turn and appends to self.log (the unified transcript).

        If the call is in half-duplex (speaker echo detected, or VOICE_HALF_DUPLEX),
        barge-in is skipped: we speak fully, drain the echo tail, then listen.
        """
        self._reg()
        _t0 = time.time()
        _tlog = ((lambda m: print("[timing] +%5.2fs %s" % (time.time() - _t0, m), flush=True))
                 if os.environ.get("VOICE_TIMING") else (lambda m: None))
        if not (self.connected and self.aud):
            return {"ok": False, "ended": self.disconnected, "user": "",
                    "interrupted": False, "spoken": "", "unsaid": ""}

        gap = (_t0 - self.last_return) if getattr(self, "last_return", None) else -1.0
        if self.half_duplex:
            _dlog("speak: half-duplex (barge-in off) gap=%.1fs" % gap)
            # echo-safe: don't listen for barge while speaking (our own voice coming
            # back off the user's speaker would trigger it). Speak fully, then listen.
            self.speak(text)
            if self.disconnected:
                self.log.append({"who": "claude", "text": text, "interrupted": False, "unsaid": ""})
                return {"ok": True, "ended": True, "interrupted": False,
                        "spoken": text, "unsaid": "", "user": ""}
            user_text = ""
            if listen_after:
                time.sleep(POST_SPEAK_DRAIN)   # let the echo of our last words die down
                user_text = self.listen(max_sec=max_wait)
            self.last_return = time.time()
            self.log.append({"who": "claude", "text": text, "interrupted": False, "unsaid": ""})
            if user_text:
                self.log.append({"who": "user", "text": user_text})
            return {"ok": True, "interrupted": False, "spoken": text, "unsaid": "",
                    "user": user_text, "ended": self.disconnected}

        wav = _tts_to_wav(text)
        dur = _wav_duration(wav)
        tts_sec = time.time() - _t0
        _tlog("TTS generated (%.1fs of audio to speak)" % dur)

        # --- phase 1: speak while a throwaway recorder senses barge-in ---
        det_wav = tempfile.mktemp(suffix=".wav")
        det = pj.AudioMediaRecorder()
        det.createRecorder(det_wav)
        self.aud.startTransmit(det)
        player = pj.AudioMediaPlayer()
        player.createPlayer(wav, pj.PJMEDIA_FILE_NO_LOOP)
        player.startTransmit(self.aud)
        start = time.time()
        interrupted_at = None
        echo_mode = False
        early_hits = 0
        # Barge threshold from the DURING-TX floor, not the pre-call noise floor: let our
        # TTS establish briefly, measure the near-end level while the user is still
        # listening, and key off that. Phone-side AEC keeps it low (~hundreds) so a real
        # barge (thousands) clears it; an echoey device pushes it up and trips the echo guard.
        time.sleep(0.4)
        tx_floor = 0.0 if self.disconnected else _tail_rms(det_wav, 0.3)
        barge_thresh = min(max(BARGE_RMS_MIN, tx_floor * 2.0), RMS_CAP)
        barge = _BargeState(barge_thresh)
        while time.time() - start < dur + 0.2:
            time.sleep(0.08)
            if self.disconnected:
                break
            if echo_mode:
                continue                            # echo detected: just finish speaking
            verdict = barge.feed(time.time() - start, _tail_rms(det_wav))
            if verdict == "echo":
                # too early to be a real interruption — maybe our own voice echoing off a
                # speaker (no phone AEC), maybe a quick interrupter. Don't cut off; keep
                # watching (still talking past EARLY_BARGE_SEC -> barge). Only after
                # ECHO_STRIKES such hits in one call give up and go half-duplex.
                self.echo_strikes = getattr(self, "echo_strikes", 0) + 1
                early_hits += 1
                if self.echo_strikes >= ECHO_STRIKES:
                    echo_mode = True
                    self.half_duplex = True
            elif verdict == "barge":
                interrupted_at = time.time() - start
                break
        try:
            player.stopTransmit(self.aud)
        except Exception:
            pass
        del player
        try:
            self.aud.stopTransmit(det)
        except Exception:
            pass
        del det
        _rm(det_wav)

        # user hung up mid-speech -> stop instantly, no transcription
        if self.disconnected:
            _rm(wav)
            return {"ok": True, "ended": True, "interrupted": interrupted_at is not None,
                    "spoken": text, "unsaid": "", "user": ""}

        interrupted = interrupted_at is not None
        _dlog("speak: gap=%.1fs tts=%.2fs audio=%.1fs tx_floor=%.0f thresh=%.0f her_rms_max=%.0f "
              "over_max=%d/%d early=%d -> %s%s"
              % (gap, tts_sec, dur, tx_floor, barge_thresh, barge.rms_max, barge.over_max,
                 barge.debounce, early_hits,
                 ("barge@%.1fs" % interrupted_at) if interrupted else "none",
                 " (now half-duplex)" if echo_mode else ""))
        _tlog("speech phase done (interrupted=%s)" % interrupted)
        if interrupted:
            spoken, unsaid = _cut_spoken(text, interrupted_at / max(dur, 0.1))
        else:
            spoken, unsaid = text, ""

        # --- phase 2: capture the user's reply ---
        user_text = ""
        if interrupted:
            # they cut in (already talking) — capture the rest the SAME fast streaming way
            # as a normal reply (whisper-server, ends ~END_SILENCE after they stop). This
            # used to record the whole interruption then transcribe it ONCE with slow
            # whisper-cli, which made post-barge replies take ~20s on a real call.
            user_text = self.listen(max_sec=max_wait)
        elif listen_after:
            # normal turn: robust whisper-driven listen (two-phase start/endpoint timing)
            user_text = self.listen(max_sec=max_wait)
        _tlog("capture/listen done -> %r (total converse engine time)" % (user_text[:40]))

        _rm(wav)
        self.last_return = time.time()
        self.log.append({"who": "claude", "text": spoken,
                         "interrupted": interrupted, "unsaid": unsaid})
        if user_text:
            self.log.append({"who": "user", "text": user_text})
        return {"ok": True, "interrupted": interrupted, "spoken": spoken,
                "unsaid": unsaid, "user": user_text, "ended": self.disconnected}

    def hangup(self):
        self._reg()
        self._bg_stop()
        try:
            if self.call:
                self.call.hangup(pj.CallOpParam(True))
                time.sleep(0.5)
        except Exception:
            pass
        self.connected = False

    def shutdown(self):
        import gc
        try:
            self.hangup()
        except Exception:
            pass
        # destroy Call/Account C++ objects BEFORE libDestroy (else pjsua asserts)
        self.call = None
        self.aud = None
        self.acc = None
        gc.collect()
        try:
            self.ep.libDestroy()
        except Exception:
            pass
        self.ep = None


if __name__ == "__main__":
    # Standalone one-turn test: call, greet, listen, echo back, hang up.
    s = CallSession()
    s.start_lib()
    print("placing call...")
    if not s.place_call():
        print("not answered"); s.shutdown(); raise SystemExit
    print("connected. speaking greeting.")
    s.speak("Hi, this is your assistant. Say something, then pause.")
    print("listening...")
    said = s.listen()
    print("HEARD:", repr(said))
    s.speak("You said: " + (said or "nothing"))
    s.hangup()
    s.shutdown()
    print("done")
