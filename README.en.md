# ringringring: your AI calls your phone for a voice conversation

[中文](README.md) | English

**Native iOS calls at zero cost.** No Mac, no $99 developer account, no need to buy or write a native iOS app, no database to configure: install the free Linphone app on your iPhone and your AI can call you.

<p align="center">
  <img src="docs/images/incoming-lockscreen.jpg" width="240" alt="Incoming call on the lock screen">
  <img src="docs/images/in-call.jpg" width="240" alt="In a call">
</p>
<p align="center">
  <img src="docs/images/incoming-in-game.jpg" width="560" alt="Incoming call while gaming">
</p>

No phone number to buy, no per-minute billing; speech recognition and speech synthesis both run on free tiers. All you need is a Linux server that is already running your AI. Calls come through even when the iPhone screen is off or Linphone is in the background.

If this helps you, a Star would mean a lot to the author ⭐

## Features

- **Two-way voice calls**: the AI calls your phone and holds a multi-turn conversation once you pick up. Your speech is transcribed to text for the AI, and the AI's reply is synthesized and played back.
- **Barge-in**: if you start speaking while the AI is talking, playback stops immediately, and the AI is told which part was spoken and which part was not.
- **Single-line lock**: multiple AI entry points on the same host share one line; dialing again during a call returns busy.
- **Privacy defaults**: logs record only connect, hang-up, error category and transcript text; the phone's network address is masked by default to reduce the risk of it entering the model's context (this is not strong isolation from the server administrator).
- **Swappable recognition and synthesis**: both speech recognition and speech synthesis are configurable; see "Swapping speech recognition and speech synthesis".

## Prerequisites

| Item | Description | Cost |
|---|---|---|
| Linux server | x86_64, Python 3.11; the host that runs Claude Code / OpenCode | Already have |
| Linphone | Phone app (App Store / app stores) | Free |
| Linphone accounts ×2 | One for the phone, one for the AI (register in the app or at subscribe.linphone.org); the AI account only needs to exist, the program only dials out and never registers with the server | Free |
| Groq API key | Speech recognition (console.groq.com) | Free tier |

### Does it work in cloud Claude Code or the official Claude app?

The calling service (the plugin program that dials and carries the audio, not your AI itself) has to run on a VPS or your own computer. Cloud Claude Code containers usually block the UDP traffic SIP needs, so calls won't go out from there.

- **VPS + Claude Code**: the easiest path; the steps below are close to one-click.
- **Your own computer**: needs some adapting for your OS (Windows / macOS / Linux); see the platform notes in `docs/`.
- **Cloud Claude Code or the official app**: needs a change to the current setup. The plugin is an MCP server, so switch it from local (stdio) to HTTPS, keep the calling service on your VPS or computer, and expose it through a Cloudflare Tunnel. Your cloud AI can then connect to it as a remote connector and place calls. If you need it now, your AI can make this change following that idea. We are also building this version; if you would rather wait, it's coming as soon as we can.

## Installation

**Option 1: install the plugin in Claude Code (recommended)**

```
/plugin marketplace add yanyan1115/ringringring
/plugin install ringringring@ringringring
```

During installation, fill in the SIP account, password and speech recognition key when prompted (if there is no prompt, run `/plugin configure ringringring@ringringring`). On first start the plugin downloads and installs its runtime by itself (about 15 seconds in our test); no script needs to be run by hand. If the server is missing system packages, the plugin fails to start and prints a `sudo apt-get install …` command; run it, then reconnect in `/mcp`.

**Option 2: manual installation**

```bash
git clone https://github.com/yanyan1115/ringringring.git && cd ringringring
# Check for missing system packages (changes nothing)
plugin-native/bin/install.sh --check
# Install; add --no-apt to skip apt and only list the missing packages
plugin-native/bin/install.sh --pjsua2 https://github.com/yanyan1115/ringringring/releases/download/v0.1.0/pjsua2-2.17-linux-x86_64-cp311.tar.gz --src .
```

With either option, the install directory is `~/.local/share/ringringring` and the config file is `~/.config/ringringring/voice.env` (mode 600).

### Configuration

When installed as a plugin, fill these in at the installation prompts. Alternatively, edit `~/.config/ringringring/voice.env` and fill in at least the following (values already set in the plugin settings take precedence):

```bash
export VOICE_SIP_ID="sip:<AI account>@sip.linphone.org"
export VOICE_SIP_USER="<AI account>"
export VOICE_SIP_PASS="<password>"                   # when configured by hand, credentials are stored only in this file
export VOICE_SIP_CALLEE="sip:<phone account>@sip.linphone.org"
export VOICE_DISPLAY_NAME="Claude"                   # caller ID name (some phones don't show emoji)
export VOICE_STT_KEY="<Groq key>"                    # or point VOICE_STT_ENV_FILE at a separate file
export VOICE_STT_LANG="zh"
```

### Network

No ports need to be opened: the server sends audio outbound first and uses STUN to traverse NAT. In our test, calls still worked both ways with inbound UDP 4000-4100 denied in the security group. If there is no sound after the call connects, first make sure Linphone is in the foreground; if there is still no sound, try opening UDP 4000-4100 (RTP audio) in the firewall or security group.

## Connecting your AI

**Claude Code**

Install the plugin as in Option 1 under "Installation". You can also load the local directory directly: `claude --plugin-dir <repo path>/plugin-native`.

Once loaded, it provides tools such as `call_start`, `converse` and `call_end`, plus the `phone-call` skill (the dial, talk and hang-up flow and the privacy conventions).

**OpenCode and other MCP clients**

Configure `plugin-native/bin/run-voice-native.sh` as a stdio MCP server. If the AI and the phone service are on different hosts, you can start the script on the phone service's host over SSH.

## Swapping speech recognition and speech synthesis

**Speech recognition (STT)**: any service compatible with the OpenAI `/audio/transcriptions` API is supported.

```bash
# Default: Groq
export VOICE_STT_URL="https://api.groq.com/openai/v1/audio/transcriptions"
export VOICE_STT_MODEL="whisper-large-v3-turbo"
# OpenAI:
# export VOICE_STT_URL="https://api.openai.com/v1/audio/transcriptions"
# export VOICE_STT_MODEL="whisper-1"
export VOICE_STT_KEY="<key>"
```

**Speech synthesis (TTS)**

```bash
# Default: Microsoft Edge TTS (zh-CN-YunxiNeural)
export VOICE_TTS="edge"
# Custom command: {text} is replaced with the text to synthesize, {out} with the output audio path.
# The command is not run through a shell, so pipes and redirection are not supported; wrap them in a script if needed.
# export VOICE_TTS_CMD="python3 my_tts.py --text {text} --out {out}"
```

## iPhone settings

1. After installing, answer one call with the app in the foreground; after that, calls come through with the screen off or the app in the background.
2. Settings → Accessibility → Touch → turn on "Prevent Lock to End Call", so an accidental press of the lock button doesn't hang up.
3. Don't save the AI's number in Contacts, otherwise the caller ID shows the contact name instead of `VOICE_DISPLAY_NAME`.
4. Can't find the AI's calls in Recents: they are filed under Unknown Callers. Settings → Apps → Phone → Call Filtering → turn off "Unknown Callers".
5. Answering on the lock screen gives the native call screen; answering while unlocked opens Linphone. Press the side button to lock and you switch to the native screen without hanging up (needs item 2).

## FAQ

| Symptom | Cause and fix |
|---|---|
| Answered but returns NO ANSWER | Linphone was suspended by the system in the background; bring the app to the foreground and try again |
| Returns LINE BUSY | Another AI entry point on the same host is on a call; no need to redial |
| Transcription slow or empty | Check the STT key and quota |
| Names and proper nouns misrecognized | A known limitation of the free model; you can swap the STT service |
| Running on a Mac | Not a one-click install: on Apple Silicon see [docs/SETUP_MACOS.md](docs/SETUP_MACOS.md), or use Docker ([docs/SETUP_DOCKER.md](docs/SETUP_DOCKER.md)); a user has it running on a Mac mini. The prebuilt package in Releases is Linux x86_64 only |
| libInit fails with 259804 on a Mac (SRTP init) | flat_namespace binds OpenSSL symbols to the system LibreSSL; use Docker, or build the SWIG bindings with `-std=c++11`. Don't disable SRTP, or the other side returns 488 |
| Does it work with Aru? | With Aru Host on a VPS and Claude Code as its driver, installing this plugin in that Claude Code should work (untested); the Aru app supports remote MCP, so once the remote version is ready you can add it directly. The calling service still has to run on a VPS or your own computer |

## Thanks

- **[Linphone](https://www.linphone.org/)**: provides free SIP accounts and the client. sip.linphone.org carries only signaling; audio flows directly between the server and the phone (not yet verified by packet capture). If your usage is heavy, please consider supporting Linphone or running your own SIP server.
- **[Ringback](https://github.com/mohitbadwal/ringback)** (Apache 2.0): the upstream of this project.

## Credits

Research and design: Cora and South; implementation: Claude

## License

[AGPL-3.0-or-later](LICENSE). This project is derived from Ringback; the upstream code is licensed under Apache 2.0, and its original license and NOTICE are kept in [LICENSE-APACHE-2.0](LICENSE-APACHE-2.0) and [NOTICE](NOTICE). The pjproject dependency is GPL-2.0-or-later.
