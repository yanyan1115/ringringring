#!/usr/bin/env python3
"""HTTPS remote MCP for the phone tool (for web connectors such as Gemini or ChatGPT).

A separate process from the stdio plugin (inbound_channel.py), reusing its call code:
- dial-out only, it does not register for incoming calls (RINGRINGRING_DIAL_ONLY=1);
  incoming calls keep ringing the local plugin as before;
- the line is guarded by the same file lock, so the two sides exclude each other;
- no `answer` tool: web connectors cannot receive server-pushed incoming-call events;
- text only: the connector gets transcripts (plus sound labels), never raw audio.
OAuth (dynamic client registration + PKCE + a connection-passphrase page) lives in remote_oauth.py.
Put an HTTPS reverse proxy or a Cloudflare tunnel in front of it; see the README.
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
from typing import Optional

os.environ["RINGRINGRING_DIAL_ONLY"] = "1"
os.environ.setdefault("VOICE_RTP_PORT", os.environ.get("VOICE_REMOTE_RTP_PORT", "4200"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import anyio  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.types import CallToolResult, TextContent  # noqa: E402

import inbound_channel as ic  # noqa: E402
import remote_oauth as ro  # noqa: E402
import voice_agent  # noqa: E402


INSTRUCTIONS = """\
This is a phone tool: it dials the user's phone (the number registered in Linphone). Once picked up it is a real voice call: what you say is spoken aloud to the user and what they say comes back to you.

How to call
1. dial(opening_line, caller_name): place the call; after pickup the opening line is spoken and the user's first reply is returned. caller_name is the name shown on the user's phone, use your own name.
2. talk(text): say one thing and wait for the reply. Use it for the whole conversation.
3. hang_up(text): say a last line (optional) and end the call. Hang up when the conversation is over or the user says goodbye; do not leave the line open.

How to speak: one or two short spoken sentences per turn, like a phone call. No markdown, lists, links or emoji (they would be read aloud).

How you hear: the user's speech arrives as text, with sound labels such as [heard: breathing] when the sound-events service is configured. This connector does not carry raw audio.

Listening quietly: quiet_listen(seconds) lets you stay silent and only listen for a while (the user is busy, resting, or just wants company). It returns what was heard in that time. Requires the server to run with VOICE_QUIET_LISTEN=1.

Markers that can appear in results
- [CALL ENDED]: the user hung up; call hang_up to wrap up.
- [NO ACTIVE CALL]: there is no call right now; dial first.
- [NO ANSWER]: no pickup. Do not redial in a loop; try later or wait for a message.
- [LINE BUSY]: another agent is already on the line; wait a while.
- [INCOMING CALL]: the user is calling in; this connector cannot take incoming calls, so wait for them to hang up and dial again.
- [SILENCE]: nothing was heard this round; say another sentence or keep waiting.

Never read out passwords or keys on a call. If the voice or content does not seem to be the user, hang up and ask by text.
"""


_provider = None


def _build_mcp() -> FastMCP:
    kwargs = {"host": os.getenv("MCP_HOST", "127.0.0.1"),
              "port": int(os.getenv("MCP_PORT", "8895"))}
    ts = ro._build_transport_security()
    if ts is not None:
        kwargs["transport_security"] = ts
    global _provider
    provider = None
    if ro._oauth_enabled():
        provider = ro.LocalOAuthProvider(ro._oauth_store_path())
        auth, prov = ro._build_oauth(provider)
        kwargs["auth"] = auth
        kwargs["auth_server_provider"] = prov
    _provider = provider
    server = FastMCP("ringringring", instructions=INSTRUCTIONS, **kwargs)
    if provider is not None:
        ro._register_oauth_routes(server, provider)
    return server


mcp = _build_mcp()

DEFAULT_HEAR = voice_agent.STT_MODE          # server default hearing mode (VOICE_STT_MODE)
DEFAULT_CALLER_NAME = os.environ.get("VOICE_REMOTE_DISPLAY_NAME", "").strip()  # used when the caller gives no name; empty = show the number
# Parameters are plain strings: Gemini's function declarations dislike anyOf/null (Optional[Literal])


def _set_hear(mode: Optional[str]) -> str:
    m = (mode or DEFAULT_HEAR).strip().lower()
    if m not in ("text", "audio", "both"):
        m = DEFAULT_HEAR
    voice_agent.STT_MODE = m                  # _listen re-reads it every turn
    return m


def _result(text: str) -> CallToolResult:
    # Text only: web connectors cannot play raw audio
    return CallToolResult(content=[TextContent(type="text", text=text)])


async def _run(fn, args: dict) -> CallToolResult:
    # pjsua2 blocks, so run it in a worker thread and keep the HTTP event loop free
    text = await anyio.to_thread.run_sync(fn, args)
    return _result(text)


@mcp.tool()
async def dial(opening_line: str, caller_name: str = "") -> CallToolResult:
    """Call the user: it rings, and after pickup opening_line is spoken and the first reply returned. Then use talk, and hang_up to end. Keep it short, like a phone call.
    caller_name = the name shown on the user's phone, use your own name; defaults to the server setting. Applies to this call only."""
    name = caller_name.strip() or DEFAULT_CALLER_NAME
    return await _run(ic.tool_dial, {"opening_line": opening_line, "caller_name": name, "hear": "text"})


@mcp.tool()
async def talk(text: str) -> CallToolResult:
    """Say one sentence on the call and listen for the reply (the user may interrupt). If it returns [CALL ENDED], call hang_up."""
    return await _run(ic.tool_talk, {"text": text})


@mcp.tool()
async def quiet_listen(seconds: float = 8) -> CallToolResult:
    """Listen without speaking: returns what the user's side produced since the last pull (transcript plus sound events), once `seconds` of audio have accumulated (default 8, max 120). Requires VOICE_QUIET_LISTEN=1."""
    return await _run(ic.tool_quiet_listen, {"seconds": seconds})


@mcp.tool()
async def hang_up(text: str = "") -> CallToolResult:
    """End the call, optionally saying a last sentence first."""
    res = await _run(ic.tool_hang_up, {"text": text})
    _set_hear(None)
    return res


def main() -> None:
    parser = argparse.ArgumentParser(description="ringringring remote MCP")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"],
                        default=os.getenv("MCP_TRANSPORT", "streamable-http"))
    args = parser.parse_args()
    if not ro._oauth_enabled() and os.getenv("MCP_HOST", "127.0.0.1") not in ("127.0.0.1", "localhost", "::1"):
        sys.exit("refusing to listen on a non-loopback address without OAuth: set MCP_OAUTH_ENABLED=1 "
                 "(or keep MCP_HOST=127.0.0.1)")
    try:
        ic.start_sip()
    except Exception as e:
        ic.log(f"SIP start failed: {type(e).__name__}: {str(e)[:200]}")
    threading.Thread(target=ic.watchdog, daemon=True).start()
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
