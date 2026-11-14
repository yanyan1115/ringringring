#!/usr/bin/env python3
"""ringringring inbound channel: the user dials this machine's SIP account from Linphone, the
phone keeps ringing, a channel event wakes the agent, the agent calls `answer`, chats with
`talk`, and ends with `hang_up`. `dial` calls the user back from the same account.

One process, three jobs: a stdio MCP server (channel + tools), a pjsua2 account registered from
inbound.env, and reuse of voice_agent.CallSession for TTS/STT/barge-in. It only imports the
outgoing-call engine (voice_agent / voice_mcp) and shares its call.lock so the two cannot
grab the line at the same time.
Only the user's own SIP address (VOICE_SIP_ID in voice.env) plus a trusted device list gets
through; anyone else is answered 486 Busy and nobody is woken. stdout is the MCP channel, so
fd 1 is pointed at stderr at start-up to keep pjsua from corrupting it.
"""
import fcntl
import base64
import json
import os
import hashlib
import re
import sys
import threading
import time

_mcp_out = os.fdopen(os.dup(1), "w", encoding="utf-8")
os.dup2(2, 1)

import pjsua2 as pj  # noqa: E402
import voice_agent  # noqa: E402
from voice_mcp import LOCK_PATH, _format_turn  # noqa: E402

SIP_ID = os.environ.get("VOICE_INBOUND_SIP_ID", "").strip()
SIP_USER = os.environ.get("VOICE_INBOUND_SIP_USER", "").strip()
SIP_PASS = os.environ.get("VOICE_INBOUND_SIP_PASS", "")
USER_ID = os.environ.get("VOICE_SIP_ID", "").strip()
RING_TIMEOUT = float(os.environ.get("VOICE_INBOUND_RING_TIMEOUT", "45"))

_out_lock = threading.Lock()
session = None
account = None
pending_since = 0.0      # >0 while ringing; reset to 0 on answer or end
in_call = False
dialing = False
_lock_fd = None
_old_calls = []          # keep Call objects alive; never destroy them inside a pjsua callback
connected_at = 0.0       # when audio connected; used for the call duration, reset after logging
call_dir = ""            # "in" | "out", set when the call connects
call_via = ""            # source passed to dial (e.g. "wechat"), written to the call log; reset at hang-up
CALL_LOG = os.environ.get("RINGRINGRING_CALL_LOG") or "~/.local/state/ringringring/calls.jsonl"


def log(msg):
    sys.stderr.write(f"[ringringring] {time.strftime('%H:%M:%S')} {msg}\n")
    sys.stderr.flush()
    # stderr is only visible to the MCP host and is gone afterwards; keep a copy of status lines (no addresses)
    try:
        with open(os.environ["VOICE_DIAG_FILE"], "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} [ringringring] {msg}\n")
    except (KeyError, OSError):
        pass


def _record_call(direction, answered, seconds, via=""):
    """Append one JSON line to the call log (four fields, plus via if dial passed one; no numbers or addresses). A write failure is only logged and never affects the call."""
    try:
        path = os.path.expanduser(CALL_LOG)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        row = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "dir": direction,
               "answered": bool(answered), "seconds": int(seconds)}
        if via:
            row["via"] = via
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except Exception as e:
        log(f"call log failed: {type(e).__name__}")


def send(obj):
    with _out_lock:
        _mcp_out.write(json.dumps(obj, ensure_ascii=False) + "\n")
        _mcp_out.flush()


def emit(content, meta):
    send({"jsonrpc": "2.0", "method": "notifications/claude/channel",
          "params": {"content": content, "meta": meta}})


def sip_id_of(uri):
    m = re.search(r"sips?:([^@;>\s]+)@([^:;>\s]+)", uri or "")
    return f"{m.group(1)}@{m.group(2)}".lower() if m else ""


def line_acquire():
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


def line_release():
    global _lock_fd
    if _lock_fd is not None:
        os.close(_lock_fd)
        _lock_fd = None


# ---------------------------------------------------------------- pjsua2

class InCall(voice_agent._Call):
    def __init__(self, acc, sess, call_id):
        pj.Call.__init__(self, acc, call_id)
        self.session = sess


TRUSTED_FILE = os.path.expanduser("~/.config/ringringring/trusted_devices")


def device_of(msg):
    """Hash of the caller's Linphone device id from the INVITE Contact line; "" if absent."""
    for line in (msg or "").splitlines():
        if not line.lower().startswith(("contact:", "m:")):
            continue
        m = re.search(r'(?:\+sip\.instance="?<?urn:uuid:|gr=urn(?::|%3a)uuid(?::|%3a))([0-9a-f-]{8,})', line, re.I)
        if m:
            return hashlib.sha256(m.group(1).lower().encode()).hexdigest()[:16]
    return ""


def trusted_devices():
    """Re-read per call so editing the list needs no reconnect. Empty = learning mode: nothing is blocked, the hash is only logged."""
    try:
        with open(TRUSTED_FILE) as f:
            return {l.split("#")[0].strip().lower() for l in f} - {""}
    except OSError:
        return set()


class InboundAccount(pj.Account):
    reg_ok = None

    def onRegState(self, prm):
        ok = 200 <= prm.code < 300
        if ok != self.reg_ok:
            log(f"register {'ok' if ok else 'failed'} ({prm.code})")
        self.reg_ok = ok

    def onIncomingCall(self, prm):
        global pending_since
        s = session
        call = InCall(self, s, prm.callId)
        _old_calls[:] = _old_calls[-4:] + [call]
        try:
            caller = call.getInfo().remoteUri
        except Exception:
            caller = ""
        is_owner = bool(sip_id_of(USER_ID)) and sip_id_of(caller) == sip_id_of(USER_ID)
        if is_owner:
            try:
                invite = prm.rdata.wholeMsg
            except Exception:
                invite = ""
            device = device_of(invite)
            allowed = trusted_devices()
            if allowed and device not in allowed:
                is_owner = False
                log(f"device={device or 'none'} not trusted")
            elif not allowed:
                log(f"device={device or 'none'} (not enforced)")
        op = pj.CallOpParam()
        if not is_owner or pending_since or in_call or dialing or not line_acquire():
            op.statusCode = pj.PJSIP_SC_BUSY_HERE
            call.hangup(op)
            log("rejected (not the user, or line busy)")
            return
        s.connected = False
        s.disconnected = False
        s.half_duplex = voice_agent.HALF_DUPLEX
        s.echo_strikes = 0
        s.last_return = None
        s.aud = None
        s.log = []
        s.call = call
        op.statusCode = pj.PJSIP_SC_RINGING
        call.answer(op)
        pending_since = time.time()
        log("user is calling -> ringing, waking session")
        emit("The user is calling you on Linphone and their phone is ringing. Use the ringringring answer tool now (pass your first sentence), then talk to chat and hang_up to end.",
             {"event": "incoming_call", "caller": "user",
              "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})


def start_sip():
    global session, account
    if not (SIP_ID and SIP_USER and SIP_PASS):
        log("inbound.env is incomplete: running the MCP server only, not registering")
        return
    session = voice_agent.CallSession()
    session.start_lib()          # same setup as outgoing calls: null sound device, STUN, TLS; its outgoing account is never registered
    acfg = pj.AccountConfig()
    name = voice_agent.SIP_DISPLAY_NAME
    acfg.idUri = f'"{name}" <{SIP_ID}>' if name else SIP_ID
    acfg.regConfig.registrarUri = "sip:" + SIP_ID.split("@", 1)[1].split(";")[0].split(">")[0]
    acfg.regConfig.timeoutSec = 300
    # remote HTTPS build: dial-out only, do not register for incoming calls (they ring the local plugin instead)
    acfg.regConfig.registerOnAdd = os.environ.get("RINGRINGRING_DIAL_ONLY", "") != "1"
    acfg.sipConfig.authCreds.append(pj.AuthCredInfo("digest", "*", SIP_USER, 0, SIP_PASS))
    acfg.sipConfig.proxies.append(voice_agent.SIP_PROXY)
    acfg.mediaConfig.srtpUse = pj.PJMEDIA_SRTP_MANDATORY
    acfg.mediaConfig.srtpSecureSignaling = 0
    account = InboundAccount()
    account.create(acfg)
    global _acfg
    _acfg = acfg                         # the remote build edits it to change the caller-ID name per call
    session.outbound_acc = session.acc   # keep the unregistered one so it is not garbage-collected
    session.acc = account                # place_call dials from the inbound account, so incoming and outgoing calls show as one contact


def watchdog():
    """The caller hung up first / rang too long / the call dropped: clear state and release the line."""
    global pending_since, in_call, connected_at, call_dir, call_via
    registered = False
    while True:
        time.sleep(1)
        s = session
        if s is None:
            continue
        if not registered:
            s._reg()
            registered = True
        if pending_since:
            if s.disconnected:
                pending_since = 0.0
                line_release()
                log("caller hung up before answer")
                _record_call("in", False, 0)
                emit("The user called but hung up before you answered.", {"event": "missed_call", "caller": "user"})
            elif time.time() - pending_since > RING_TIMEOUT:
                pending_since = 0.0
                try:
                    op = pj.CallOpParam()
                    op.statusCode = pj.PJSIP_SC_BUSY_HERE
                    s.call.hangup(op)
                except Exception as e:
                    log(f"timeout reject failed: {type(e).__name__}")
                line_release()
                log("ring timeout")
                _record_call("in", False, 0)
        elif in_call and s.disconnected:
            in_call = False
            # dropped before audio connected ([NO MEDIA] etc.) is logged as not answered
            _record_call(call_dir, bool(connected_at), round(time.time() - connected_at) if connected_at else 0, call_via)
            connected_at = 0.0
            call_via = ""
            call_dir = "in"
            s._bg_stop()
            line_release()


# ---------------------------------------------------------------- tools

def tool_answer(args):
    global pending_since, in_call, connected_at, call_dir
    s = session
    if s is None or not pending_since or s.disconnected:
        return "[NO INCOMING CALL] - nothing is ringing right now (the caller may have hung up)."
    s._reg()
    op = pj.CallOpParam(True)
    op.statusCode = pj.PJSIP_SC_OK
    s.call.answer(op)
    pending_since = 0.0
    in_call = True
    call_dir = "in"
    end = time.time() + 10
    while time.time() < end:
        time.sleep(0.1)
        if s.connected and s.aud is not None:
            connected_at = time.time()
            call_dir = "in"
            time.sleep(0.4)
            s.noise_floor = s._measure_rms(0.6)
            if not voice_agent.USE_GROQ:
                voice_agent._wsrv_warm()
            s._bg_start()
            return _format_turn(s.speak_interruptible(args["opening_line"]))
        if s.disconnected:
            return "[CALL ENDED]"
    s.hangup()
    return "[NO MEDIA] - no audio 10 s after answering; hung up."


_acfg = None
DEFAULT_HEAR = voice_agent.STT_MODE      # server default hearing mode (VOICE_STT_MODE)


def _set_hear(mode):
    """Pick the hearing mode once for the whole call at dial time; missing/invalid = default. _listen re-reads STT_MODE every turn."""
    m = (mode or DEFAULT_HEAR).strip().lower()
    if m not in ("text", "audio", "both"):
        m = DEFAULT_HEAR
    voice_agent.STT_MODE = m
    return m


def _set_caller_name(name):
    """Change the caller-ID display name before dialing (only the From display name; the address stays). No-op if unchanged, to avoid a needless re-registration."""
    if account is None or _acfg is None:
        return
    name = "".join(ch for ch in (name or "") if ch not in '"<>\\\r\n').strip()[:40]
    uri = f'"{name}" <{SIP_ID}>' if name else SIP_ID
    if uri == _acfg.idUri:
        return
    _acfg.idUri = uri
    if session is not None:
        session._reg()                   # tools run in new threads; register the thread before touching pjlib (an unregistered thread crashes it)
    try:
        account.modify(_acfg)
    except Exception as e:
        log(f"caller name not applied: {type(e).__name__}")


def tool_dial(args, on_connected=None):
    global in_call, dialing, connected_at, call_dir, call_via
    s = session
    if s is None:
        return "[NOT CONFIGURED]"
    if pending_since:
        return "[INCOMING CALL] - the user is calling in right now; use answer."
    if in_call or dialing or not line_acquire():
        return "[LINE BUSY] - the line is in use; try again later."
    dialing = True
    try:
        s.log = []
        _set_hear(args.get("hear"))
        _set_caller_name(args.get("caller_name") or voice_agent.SIP_DISPLAY_NAME)
        via = "".join(c for c in str(args.get("via") or "") if c.isalnum() or c in "-_")[:32]
        placed = s.place_call(callee=USER_ID)
        if placed:
            in_call = True
            connected_at = time.time()
            call_dir = "out"
            call_via = via
            if on_connected is not None:     # remote build: start whole-call recording once connected
                try:
                    on_connected()
                except Exception as e:
                    log(f"on_connected failed: {type(e).__name__}")
    finally:
        dialing = False
        if not in_call:
            line_release()
    if not placed:
        _record_call("out", False, 0, via)
        return "[NO ANSWER] - no pickup; do not redial in a loop."
    return _format_turn(s.speak_interruptible(args["opening_line"]))


def tool_talk(args):
    s = session
    if s is None or not in_call:
        return "[NO ACTIVE CALL]"
    return _format_turn(s.speak_interruptible(args["text"]))


def tool_quiet_listen(args):
    s = session
    if s is None or not in_call:
        return "[NO ACTIVE CALL]"
    out = s.quiet_listen(args.get("seconds") or 8)
    if s.disconnected:
        return (out + "\n" if out else "") + "[CALL ENDED]"
    return out or "[SILENCE]"


def tool_wait_for_voice(args):
    s = session
    if s is None or not in_call:
        return "[NO ACTIVE CALL]"
    out = s.wait_for_voice(args.get("max_sec") or 10)
    if s.disconnected:
        return (out + "\n" if out else "") + "[CALL ENDED]"
    return out or "[SILENCE]"


def tool_hang_up(args):
    s = session
    if s is None or not in_call:
        return "[NO ACTIVE CALL]"
    text = args.get("text") or ""
    if text and s.connected:
        s.speak(text)
        if not s.disconnected:
            time.sleep(1.0)
    s.hangup()
    _set_hear(None)
    return "call ended"


def tool_set_hearing(args):
    m = _set_hear(args.get("mode"))
    return f"hearing mode set to {m} until hang-up."


TOOLS = {
    "answer": (tool_answer, "Answer the incoming call (call this after an incoming_call event). Says the opening line and returns the caller's reply. Keep it short, like a phone call.",
               {"opening_line": {"type": "string"}}, ["opening_line"]),
    "dial": (tool_dial, "Call the user from this machine's SIP account (rings, says the opening line once picked up, returns the reply), then use talk / hang_up. Keep it short. "
             "caller_name = the name shown on the user's phone for this call only; defaults to the configured display name. "
             "hear = how to hear the user for the whole call: text (transcript) / audio (raw audio) / both; defaults to the server setting. "
             "Choose text if you cannot take audio input. "
             "via = where the request for this call came from, e.g. wechat when the user asked for the call in WeChat or you are reporting a job sent from WeChat (with ringbot this makes a call-duration bubble appear in WeChat); leave it out otherwise.",
             {"opening_line": {"type": "string"}, "caller_name": {"type": "string"},
              "hear": {"type": "string", "enum": ["text", "audio", "both"]},
              "via": {"type": "string"}}, ["opening_line"]),
    "talk": (tool_talk, "Say one sentence on the call and listen for the reply; the caller can interrupt. If it returns [CALL ENDED], call hang_up.",
             {"text": {"type": "string"}}, ["text"]),
    "quiet_listen": (tool_quiet_listen,
                     "Listen without speaking: returns what the caller's side produced since the last pull (transcript plus [heard: ...] sound labels when the sound-events service is configured). "
                     "Returns once `seconds` of audio have accumulated (default 8, max 120); one line per voiced chunk as 'm:ss content'; all silence = [SILENCE]. "
                     "You can do other things in between and come back; nothing in between is lost. Requires VOICE_QUIET_LISTEN=1.",
                     {"seconds": {"type": "number"}}, []),
    "wait_for_voice": (tool_wait_for_voice,
                       "Wait on the call without speaking and return AT ONCE when the caller makes a sound (with rms and sound labels), "
                       "or [SILENCE] after max_sec seconds (default 10, max 600). The default is short so that typed messages reach you within about 10 s. "
                       "Waiting costs nothing and a sound interrupts it immediately, so it is faster than quiet_listen. "
                       "It does not transcribe and does not consume the recording: call quiet_listen afterwards if you want the words. Requires VOICE_QUIET_LISTEN=1.",
                       {"max_sec": {"type": "number"}}, []),
    "hang_up": (tool_hang_up, "End the call, optionally saying a last sentence first.",
                {"text": {"type": "string"}}, []),
    "set_hearing": (tool_set_hearing, "Change the hearing mode mid-call (text / audio / both), e.g. to turn raw audio off. Normally unnecessary: the mode chosen at dial time covers the whole call and resets to the default at hang-up.",
                    {"mode": {"type": "string", "enum": ["text", "audio", "both"]}}, ["mode"]),
}


def run_tool(mid, name, args):
    try:
        text = TOOLS[name][0](args)
        content = [{"type": "text", "text": text}]
        s = session
        audio = getattr(s, "last_audio", b"") if s is not None else b""
        if audio and name in ("answer", "dial", "talk") and voice_agent.STT_MODE != "text":
            s.last_audio = b""   # hand it over once; lives in memory only
            content.append({"type": "audio", "mimeType": "audio/wav",
                            "data": base64.b64encode(audio).decode("ascii")})
        send({"jsonrpc": "2.0", "id": mid, "result": {"content": content}})
    except Exception as e:
        log(f"tool {name} failed: {type(e).__name__}")
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": f"[ERROR] {type(e).__name__}"}], "isError": True}})


def handle(msg):
    mid, method = msg.get("id"), msg.get("method")
    params = msg.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": params.get("protocolVersion") or "2025-06-18",
            "capabilities": {"experimental": {"claude/channel": {}}, "tools": {"listChanged": False}},
            "serverInfo": {"name": "ringringring", "version": "0.2.0"},
            "instructions": ("When the user dials this account from Linphone, their phone keeps ringing and this server wakes you with <channel event=\"incoming_call\">. "
                             "Call answer immediately, then talk to chat and hang_up to end. To call the user yourself, use dial (same account, so it is the same contact on their phone). "
                             "Hearing mode: pick one with `hear` at dial time (text / audio / both) for the whole call; use set_hearing only to change it mid-call. "
                             "Choose text if you cannot take audio input, or if your client does not receive audio."),
        }})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [
            {"name": n, "description": t[1],
             "inputSchema": {"type": "object", "properties": t[2], "required": t[3]}}
            for n, t in TOOLS.items()]}})
    elif method == "tools/call":
        name = params.get("name")
        if name not in TOOLS:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool"}})
            return
        threading.Thread(target=run_tool, args=(mid, name, params.get("arguments") or {}), daemon=True).start()
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}})


def main():
    # push on voice: let voice_agent's sentry thread push straight into this channel,
    # the same way watchdog pushes missed_call.
    try:
        voice_agent.set_push_sink(emit)
    except Exception as e:
        log(f"push sink not set: {type(e).__name__}")
    try:
        start_sip()
    except Exception as e:
        log(f"SIP start failed: {type(e).__name__}: {str(e)[:200]}")
    threading.Thread(target=watchdog, daemon=True).start()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            handle(json.loads(line))
        except Exception as e:
            log(f"bad message: {type(e).__name__}")
    log("stdin closed, exiting")
    os._exit(0)


if __name__ == "__main__":
    main()
