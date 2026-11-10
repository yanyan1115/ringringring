#!/usr/bin/env python3
"""ringback-voice MCP — exposes a live phone call as speak/listen tools.

The calling Claude session IS the conversational brain: it calls call_start to
ring the user, then alternates listen()/speak() each turn, and call_end() when
done. No LLM API key — the model already running this tool does the thinking.

Requires pjsua2 (built from source) + whisper.cpp + Piper/say (TTS) + ffmpeg.
Launched via run_voice_mcp.sh (macOS) or run_voice_mcp.py (cross-platform), which set
PYTHONPATH + the OS dynamic-linker path so `import voice_agent` (pjsua2) resolves.
"""
import fcntl
import os
import time
from mcp.server.fastmcp import FastMCP
import functools
import voice_agent
from privacy import redact

mcp = FastMCP("ringback-voice")


def _safe_tool(fn):
    """Exceptions become tool results the model reads; scrub addresses out of them."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:
            raise RuntimeError(redact(f"{type(e).__name__}: {e}")) from None
    return wrapper
_session = None

# One phone line per user: several agent entries (CC, OpenCode, ...) can each run their
# own MCP process on the same host, all dialing the same phone. An flock held from
# call_start until the call is over makes a second call_start answer "line busy"
# instead of ringing the phone twice. The kernel drops it if the process dies.
LOCK_PATH = os.environ.get("VOICE_CALL_LOCK", "").strip() or os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
    "ringringring", "call.lock")
_lock_fd = None


def _line_acquire() -> bool:
    global _lock_fd
    if _lock_fd is not None:
        return True
    os.makedirs(os.path.dirname(LOCK_PATH), mode=0o700, exist_ok=True)
    fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return False
    _lock_fd = fd
    return True


def _line_release() -> None:
    global _lock_fd
    if _lock_fd is not None:
        try:
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(_lock_fd)
            _lock_fd = None


def _ended(result: str) -> str:
    """Pass a tool result through, freeing the line if the call is over."""
    if _session is not None and (_session.disconnected or not _session.connected):
        _line_release()
    return result


def _get():
    global _session
    if _session is None:
        _session = voice_agent.CallSession()
        _session.start_lib()
    return _session


def _listen_result(s) -> str:
    """Listen for one turn and format the result for the model."""
    if s.disconnected or not s.connected:
        return "[CALL ENDED]"
    text = s.listen()
    if s.disconnected:
        return "[CALL ENDED]"
    return text or "[SILENCE]"


def _format_turn(r: dict) -> str:
    """Turn a speak_interruptible() result into a clear instruction for the model."""
    if not r.get("ok"):
        return "[CALL ENDED]" if r.get("ended") else "[NO ACTIVE CALL]"
    if r["interrupted"]:
        msg = f'INTERRUPTED — you only got to say: "{r["spoken"]}".'
        if r["unsaid"]:
            msg += f' You had NOT yet said: "{r["unsaid"]}".'
        msg += f' The user talked over you with: "{r["user"] or "[unclear]"}".'
        msg += " Decide whether to address what they said or finish your point — your call."
        if r.get("ended"):
            msg += " [CALL ENDED]"
        return msg
    if r.get("ended"):
        return (f'User replied: "{r["user"]}" then the call ENDED.'
                if r["user"] else "[CALL ENDED]")
    return f'User replied: "{r["user"]}"' if r["user"] else "[SILENCE] — user said nothing; you may prompt again."


@mcp.tool()
@_safe_tool
def call_start(opening_line: str) -> str:
    """Ring the user's phone, speak an opening line, and return their first reply.

    Use this when you need to TALK with the user, not just notify them (for a
    fire-and-forget alert, use alert_me instead). It dials their phone; when they
    answer it speaks `opening_line` aloud, then listens and returns what they say.

    CONVERSATION PROTOCOL — just two tools, one call per turn:
      1. reply = call_start("Hi, it's your Claude assistant — your prod deploy
         failed. Want me to walk you through it?")   # dials, speaks, returns reply
      2. reply = converse("<your short response>")    # speaks + listens, each turn
      3. repeat step 2 for every turn
      4. call_end("<closing line>")  to speak your FINAL message and hang up in one
         step (no awkward wait), OR call_end() with no text when the user signals
         they're done ("bye", "that's all") OR a reply is "[CALL ENDED]"

    Keep every spoken line SHORT and conversational — 1-2 sentences, like a real
    phone call. Returns the user's first words, or "[NO ANSWER]" if they didn't
    pick up, or "[SILENCE]" if they answered but said nothing.
    """
    if not _line_acquire():
        return ("[LINE BUSY] — another agent is on the phone with the user right now. "
                "Do not retry immediately; try again later or message them instead.")
    try:
        s = _get()
        s.log = []                       # fresh conversation
        placed = s.place_call()
    except Exception:
        _line_release()
        raise
    if not placed:
        _line_release()
        return "[NO ANSWER] — the user did not pick up. Do not retry immediately."
    return _ended(_format_turn(s.speak_interruptible(opening_line)))


@mcp.tool()
@_safe_tool
def converse(text: str) -> str:
    """Say `text` to the user AND listen — one interruptible turn, one call.

    The main conversation tool. It speaks your line WHILE listening: if the user
    talks over you, it stops immediately and tells you how far you got and what
    they said (barge-in). If you finish uninterrupted, it listens for their reply.

    Returns one of:
      - 'User replied: "..."'                         (normal turn)
      - 'INTERRUPTED — you only got to say "..."; you had NOT yet said "..."; the
         user talked over you with "...". Decide ...'  (barge-in — you choose
         whether to answer their interruption or finish your point)
      - '[SILENCE]'                                    (they said nothing)
      - '[CALL ENDED]'                                 (they hung up -> call_end)

    Speak like a human on the phone: 1-2 short sentences. Read aloud by TTS, so
    NEVER include raw logs, stack traces, codes, UUIDs, or JSON — give a plain
    summary (offer to send exact detail via alert_me). Use get_conversation() any
    time to see the full transcript so far.
    """
    s = _get()
    if not s.connected:
        return _ended("[NO ACTIVE CALL] — call call_start first")
    return _ended(_format_turn(s.speak_interruptible(text)))


@mcp.tool()
@_safe_tool
def get_conversation() -> str:
    """Return the full call transcript so far — both sides, with interruptions.

    Gives you the complete conversation in one place so you can see exactly what
    was said, by whom, and where you got cut off, then decide how to continue.
    """
    s = _session
    if s is None or not getattr(s, "log", None):
        return "(no conversation yet)"
    out = []
    for e in s.log:
        if e["who"] == "claude":
            line = f'CLAUDE: "{e["text"]}"'
            if e.get("interrupted"):
                line += f'  [cut off; had not said: "{e.get("unsaid", "")}"]'
        else:
            line = f'USER: "{e["text"]}"'
        out.append(line)
    return "\n".join(out)


@mcp.tool()
@_safe_tool
def speak(text: str) -> str:
    """Speak `text` aloud to the user on the live call (text-to-speech).

    Keep it to 1-2 short sentences. Returns once finished speaking. After
    speaking, normally call listen() to get the user's response. Requires an
    active call started with call_start.

    IMPORTANT — this is read aloud by a TTS voice, so speak like a human on the
    phone: a short PLAIN-LANGUAGE summary. NEVER read raw logs, stack traces,
    error codes, UUIDs, file paths, or JSON aloud — they sound like gibberish.
    Say "the deploy failed on the sentiment scoring step" — NOT "ADF_run_8841
    node[3] threw NullPointerException at line 412". If the user wants the exact
    detail, offer to send it as a message (alert_me) instead.
    """
    s = _get()
    if not s.connected:
        return "[NO ACTIVE CALL] — call call_start first"
    s.speak(text)                       # PURE TTS: say it and return, do NOT listen
    if s.disconnected:
        return _ended("[CALL ENDED]")
    return ('spoke (call still open). Call listen() for their reply, or '
            'call_end("final line") to deliver a closing line and hang up.')


@mcp.tool()
@_safe_tool
def listen() -> str:
    """Listen to the user on the live call; return what they said, transcribed.

    Records until the user stops talking (~1.2s of silence) or 12s max, then
    transcribes with whisper. Returns:
      - the user's words (text), or
      - "[SILENCE]" if they didn't say anything (you may prompt them again), or
      - "[CALL ENDED]" if they hung up — stop the loop and call call_end().
    Alternate listen() and speak() for each conversational turn.
    """
    s = _get()
    if s.disconnected or not s.connected:
        return _ended("[CALL ENDED]")
    text = s.listen()
    if s.disconnected:
        return _ended("[CALL ENDED]")
    return text or "[SILENCE]"


@mcp.tool()
@_safe_tool
def quiet_listen(seconds: float = 8.0) -> str:
    """Listen WITHOUT speaking: return what the user did since the last call of this tool.

    The engine records the user's side in the background for the whole call, so you can
    stay silent (type elsewhere, run other tools) and come back. Waits until at least
    `seconds` (default 8, max 120) of audio has built up, then returns one line per
    10 s of sound: "m:ss words[heard: ...]" (sound labels need the sound-events service).
    "[SILENCE]" if they were quiet, "[CALL ENDED]" if they hung up.
    """
    s = _get()
    if s.disconnected or not s.connected:
        return _ended("[CALL ENDED]")
    out = s.quiet_listen(seconds)
    if s.disconnected:
        return _ended((out + "\n" if out else "") + "[CALL ENDED]")
    return out or "[SILENCE]"


@mcp.tool()
@_safe_tool
def call_end(text: str = "") -> str:
    """Hang up the current call — optionally speaking one final line first.

    If `text` is given, it speaks that line, waits ~1 second so the words fully
    reach the user before the line drops, then hangs up — with NO waiting for a
    reply. Use this for your closing message ("Talk soon", "I'll call back when
    it's done"). Call with no argument to just hang up.
    """
    s = _get()
    if text and s.connected:
        s.speak(text)            # pure TTS; returns AT ONCE if the user hangs up mid-line
        if s.disconnected:
            s.hangup()
            _line_release()
            return "[CALL ENDED] — the user had already hung up; closing line not delivered"
        time.sleep(1.0)          # let the final words fully land before hanging up
    try:
        s.hangup()
    finally:
        _line_release()
    return "spoke final line and hung up" if text else "call ended"


@mcp.tool()
@_safe_tool
def call_status() -> str:
    """Report whether a call is currently active (debug/sanity)."""
    s = _session
    if s is None:
        return "engine not started; no call"
    return f"connected={s.connected} disconnected={s.disconnected}"


def main():
    mcp.run()


if __name__ == "__main__":
    main()
