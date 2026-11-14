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
- **Incoming calls** (v0.2.0, optional plugin): you can also call the AI from Linphone.
- **Quiet listen** (v0.2.0, off by default): during a call the AI can stay silent and just listen to your side.
- **HTTPS remote version** (v0.2.0): works with the Claude, Gemini and ChatGPT apps and websites.

## Prerequisites

| Item | Description | Cost |
|---|---|---|
| Linux server | x86_64, Python 3.11; the host that runs Claude Code / OpenCode | Already have |
| Linphone | Phone app (App Store / app stores) | Free |
| Linphone account | 1 required (for your phone; register in the app or at subscribe.linphone.org); a 2nd one for the AI is optional, needed only if you want to call the AI yourself (see "Incoming calls"); with only one, set `VOICE_REMOTE_ACCOUNT=voice` for the HTTPS remote version | Free |
| Speech recognition key | ElevenLabs recommended (elevenlabs.io; the free plan gives 10,000 credits a month and is very accurate); Groq (console.groq.com) also works. With both, ElevenLabs is tried first and Groq is the fallback | Free tier |

### Does it work in cloud Claude Code or the official Claude app?

The calling service (the plugin program that dials and carries the audio, not your AI itself) has to run on a VPS or your own computer. Cloud Claude Code containers usually block the UDP traffic SIP needs, so calls won't go out from there.

- **VPS + Claude Code**: the easiest path; the steps below are close to one-click.
- **Your own computer**: needs some adapting for your OS (Windows / macOS / Linux); see the platform notes in `docs/` (note: these are the old upstream Ringback guides, not updated for v0.2.0).
- **Cloud Claude Code or the official apps and websites**: use the v0.2.0 HTTPS remote version (see "HTTPS remote MCP with OAuth" below). The calling service still runs on your VPS or computer; a Cloudflare Tunnel gives it an https address, and your cloud AI connects to it as a remote connector to place calls. For now it is configured by hand; a one-click installer is planned.

## Installation

**Option 1: install the plugin in Claude Code (recommended)**

```
/plugin marketplace add yanyan1115/ringringring
/plugin install ringringring@ringringring
```

During installation, fill in the SIP account, password and speech recognition key when prompted (if there is no prompt, run `/plugin configure ringringring@ringringring`). The key field in the prompt is for Groq; to use ElevenLabs, put `VOICE_STT_11LABS_KEY` in `~/.config/ringringring/voice.env` (see "Configuration"). On first start the plugin downloads and installs its runtime by itself (about 15 seconds in our test); no script needs to be run by hand. If the server is missing system packages, the plugin fails to start and prints a `sudo apt-get install …` command; run it, then reconnect in `/mcp`.

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

When installed as a plugin, fill these in at the installation prompts. Alternatively, edit `~/.config/ringringring/voice.env` (values already set in the plugin settings take precedence). The three SIP lines are required, and at least one speech recognition key is strongly recommended (without one only local whisper.cpp is left, see below):

```bash
export VOICE_SIP_ID="sip:<your account>@sip.linphone.org"   # the account logged in on your phone; the AI calls you with it
export VOICE_SIP_USER="<your account>"
export VOICE_SIP_PASS="<password>"                   # when configured by hand, credentials are stored only in this file
# export VOICE_SIP_CALLEE="sip:<other account>@sip.linphone.org"  # optional: only to call a different address; defaults to VOICE_SIP_ID (main plugin only; dial in the incoming-call plugin and the remote version always calls VOICE_SIP_ID)
export VOICE_DISPLAY_NAME="Claude"                   # caller ID name (some phones don't show emoji)
export VOICE_STT_11LABS_KEY="<ElevenLabs key>"       # recommended; or point VOICE_STT_11LABS_ENV_FILE at a separate file
export VOICE_STT_KEY="<Groq key>"                    # optional fallback if ElevenLabs fails; or use VOICE_STT_ENV_FILE
export VOICE_STT_LANG="zh"
export VOICE_STUN="stun.linphone.org:3478"            # lets audio cross NAT; the plugin fills this in by default
```

### Network

No ports need to be opened: the server sends audio outbound first and uses STUN to traverse NAT. In our test, calls still worked both ways with inbound UDP 4000-4100 denied in the security group. If there is no sound after the call connects, first make sure Linphone is in the foreground; if there is still no sound, try opening UDP 4000-4100 (RTP audio) in the firewall or security group, plus UDP 4200-4300 if you run the HTTPS remote version.

If the call connects but neither side hears anything, and the server keeps sending audio without receiving a single packet back, the phone is most likely on a proxy or VPN that does not forward UDP (common for users in mainland China). Try in this order: (1) turn the proxy off and call directly; a user in mainland China confirmed calls work with no proxy at all. (2) If you need the proxy on, add direct (bypass) rules for linphone.org and your own server's IP; this one has not been tested yet. (3) If neither works, relay the call audio through your server; the extra hop adds some latency.

## Connecting your AI

**Claude Code**

Install the plugin as in Option 1 under "Installation". You can also load the local directory directly: `claude --plugin-dir <repo path>/plugin-native`.

Once loaded, it provides tools such as `call_start`, `converse` and `call_end`, plus the `phone-call` skill (the dial, talk and hang-up flow and the privacy conventions).

**OpenCode and other MCP clients**

Configure `plugin-native/bin/run-voice-native.sh` as a stdio MCP server. If the AI and the phone service are on different hosts, you can start the script on the phone service's host over SSH.

## Swapping speech recognition and speech synthesis

**Speech recognition (STT)**: ElevenLabs is recommended, see "Speech recognition: recommendation and our setup" below. The fallback layer accepts any service compatible with the OpenAI `/audio/transcriptions` API:

```bash
# Default address: Groq
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

## What's new in v0.2.0

Everything here is optional; the original outgoing calls work without any of it. Variable names and examples are at the end of `voice.env.example`.

### Incoming calls (the `plugin-inbound` plugin)

Until now only the AI could call you. Now you can call the AI: dial the AI's number in Linphone, your phone keeps ringing, the AI gets an `incoming_call` event, calls `answer`, chats with `talk` and ends with `hang_up`. When the AI wants to reach you it uses `dial` (same number, so it is the same contact on your phone).

```
/plugin install ringringring-inbound@ringringring
```

Install the main ringringring plugin first (it provides pjsua2 and the engine code). The AI's account is configured in `~/.config/ringringring/inbound.env` (`VOICE_INBOUND_SIP_ID`, `_USER`, `_PASS`, plus `VOICE_INBOUND_RING_TIMEOUT` and `VOICE_INBOUND_RTP_PORT`); `VOICE_SIP_ID` in `voice.env` is your own address, used to recognize you. This account does register with the Linphone server, otherwise it could not receive calls.

**Two identity checks.** Anyone who knows a SIP address can call it, and caller ID can be spoofed, so an incoming call has to pass both: (1) the caller's `user@host` must match `VOICE_SIP_ID` exactly; (2) the caller's device must be listed in `~/.config/ringringring/trusted_devices` (one hashed Linphone device id per line, only a sha256 prefix is stored, read on every call so edits need no reconnect). Calls that fail either check get a busy tone and never wake the AI. While the list is empty **nothing is blocked**: the log just shows `device=<hash> (not enforced)` so you can copy your own hash into the list. In other words, with an empty list only the SIP address is checked, and that can be spoofed, so please fill the list in. A device id is not cryptographic proof, so do not say keys or other private details on a call.

**Call log.** After every call the incoming-call plugin appends one line `{"at","dir","answered","seconds"}` to `RINGRINGRING_CALL_LOG` (default `~/.local/state/ringringring/calls.jsonl`, mode 600), with no numbers or addresses; if the AI passed `via` to `dial` (e.g. `"wechat"`) the line also gets a `"via"` field. Other programs can read it: [ringbot](https://github.com/yanyan1115/ringbot) uses it to post a call-duration bubble in WeChat.

**Wait a few seconds before dialing.** Right after you switch back to Linphone it has not finished connecting, so the first INVITE carries no device id (`device=none` in the log) and is rejected: it rings once and drops. Wait a few seconds and dial again. Reinstalling Linphone changes the device id, so add the new one to the list.

### Quiet listen, wait for voice, push on voice (off by default)

Lets the AI stay silent on a call and only listen to your side. With `VOICE_QUIET_LISTEN=1` a background recorder runs while the call is up (memory and temp files only; the incoming-call plugin drops it at hang-up, the main plugin when the AI calls `call_end` or the next call starts). `quiet_listen` is in both the main and the incoming-call plugin; `wait_for_voice` and push-on-voice below are only in the incoming-call plugin `plugin-inbound`.

- `quiet_listen(seconds)`: returns "m:ss transcript" lines once that many seconds have collected, or `[SILENCE]`.
- `wait_for_voice(max_sec)`: waits without speaking and returns the moment a sound arrives (no transcript; the audio stays in the recording so `quiet_listen` can still fetch the words).
- Add `VOICE_PUSH_ON_VOICE=1` and a notification is pushed to the AI as soon as a sound starts, so it does not need to poll.

All of these default to off. Because they record continuously, decide for yourself whether to turn them on.

### Hearing modes (text / raw audio)

`VOICE_STT_MODE` has three hearing modes: `text` gives the AI a transcript only (default); `audio` attaches your raw voice and gives no text; `both` gives both. The AI can pick one for a whole call with `dial(hear=...)` and change it mid-call with `set_hearing`. If your AI cannot take audio input, use `text`. `audio`/`both`, `dial(hear=...)` and `set_hearing` exist only in the incoming-call plugin `plugin-inbound`; the main plugin never attaches audio: with `audio` the AI only gets a placeholder and no words, and `both` behaves like `text`, so keep `text` there.

### Speech recognition: recommendation and our setup

We recommend ElevenLabs for speech recognition: the free plan gives 10,000 credits a month, which is plenty for phone transcription, and it is very accurate. Set `VOICE_STT_11LABS_KEY` (or `VOICE_STT_11LABS_ENV_FILE`); batch and realtime streaming variants are described in `voice.env.example`. Streaming also needs `websocket-client`: `~/.local/share/ringringring/venv/bin/pip install websocket-client`; without it, it falls back to batch.

The author runs three layers, each one used only if the one before fails:
1. ElevenLabs Scribe (code default `scribe_v2`; the language code is ISO-639-3, so Chinese is `zho`, not `zh`)
2. Groq `whisper-large-v3-turbo`
3. Local whisper.cpp: the default model is an English-only `.en` model. It keeps turn-taking working but **cannot really transcribe Chinese**, so treat it as a last resort.

An ElevenLabs key alone or a Groq key alone both work. With neither, only local whisper.cpp is left: the plugin does not install it, so you would need to install whisper.cpp and a model yourself, and the default model cannot transcribe Chinese. Not recommended.

### HTTPS remote MCP with OAuth

Works with the Claude, Gemini and ChatGPT apps and websites.

For AIs that can only use HTTPS remote connectors, such as cloud Claude Code or the official apps and websites: `plugin-inbound/bin/run_remote.sh` (that is, `remote_http.py`) serves `dial`, `talk`, `quiet_listen` and `hang_up` as a remote MCP on your own host. It is dial-out only (no answering) and returns text only, never raw audio. Authentication is OAuth (dynamic client registration, PKCE, plus a "connection passphrase" page where you type the passphrase you chose).

The remote version dials out from the second account (the one in `inbound.env`) by default; with only one account, set `VOICE_REMOTE_ACCOUNT=voice` to use the voice.env account. Setup: keep the service on `127.0.0.1` and forward a domain to it with a Cloudflare Tunnel (or any HTTPS reverse proxy); set `MCP_OAUTH_ENABLED=1`, `MCP_OAUTH_ISSUER_URL`, `MCP_OAUTH_CONNECT_SECRET` (the passphrase, keep it secret) and optionally `MCP_ALLOWED_HOSTS`. OAuth tokens are stored under `~/.local/share/ringringring/oauth/` by default. Without OAuth the service refuses to listen on anything but loopback. A one-click installer is planned; for now this is manual.

Credits: the OAuth part of the https remote MCP is adapted from South's project.

### Sound label hook (experimental, off by default)

Optional: each turn's audio (and each chunk picked up by quiet listen / wait for voice) is POSTed to a sound classifier you run yourself, and the labels it returns (for example breathing or coughing) are appended to the transcript as `[heard: ...]`. **No classifier is included**, only the calling side: POST a WAV (header `X-Sound-Key`; the first 500 characters of the transcript are sent too, URL-encoded in header `X-Sound-Text`, so only point it at a service you trust), get back a JSON list of label strings. Set `VOICE_SOUND_EVENTS=1`, `VOICE_SOUND_URL` and `VOICE_SOUND_KEY` on the call engine; if any is missing it stays off.

### Phone microphone tip

If the AI cannot hear very quiet sounds on your side (breathing, muttering), turn on the **speakerphone** in Linphone during the call and choose the **Wide Spectrum** microphone mode in the system (iPhone: pull down Control Center during the call and tap "Mic Mode"). The phone then keeps a wider range of sound instead of isolating just the voice, so faint sounds come through better.

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
| Running on a Mac | Not a one-click install: on Apple Silicon see [docs/SETUP_MACOS.md](docs/SETUP_MACOS.md), or use Docker ([docs/SETUP_DOCKER.md](docs/SETUP_DOCKER.md)); a user has it running on a Mac mini. Note that the Docker image in this repo is still the upstream version and does not include the v0.2.0 features; an update is planned. The prebuilt package in Releases is Linux x86_64 only |
| libInit fails with 259804 on a Mac (SRTP init) | flat_namespace binds OpenSSL symbols to the system LibreSSL; use Docker (old upstream image, no v0.2.0 features), or build the SWIG bindings with `-std=c++11`. Don't disable SRTP, or the other side returns 488 |
| Does it work with a self-built frontend? | If the frontend's backend runs Claude Code or similar on a VPS, installing this plugin there should work (untested); if the frontend supports remote MCP, you can add the HTTPS remote version directly (see above). The calling service still has to run on a VPS or your own computer |

### If you write or change the pjsua2 config yourself

The plugin already sets all of this. You only run into these when writing your own code or a `voice.env` by hand.

| Symptom | Cause and fix |
|---|---|
| Registration times out, or it succeeds and then dialing goes nowhere | An account that only dials out should not register: `acfg.regConfig.registrarUri = ""` |
| Dialing does nothing and the logs show no reason | Create a TLS transport first, and include the transport in the proxy: `acfg.sipConfig.proxies.append("sip:sip.linphone.org;transport=tls")` |
| The process exits right after `makeCall()` with code 134, no exception or stack trace (reported by a user) | Set SRTP on the account's media config: `acfg.mediaConfig.srtpUse = pj.PJMEDIA_SRTP_MANDATORY`. `EpConfig` and `TransportConfig` have no such setting |
| The call connects and rings, but neither side hears anything, and the SDP `c=` line shows a private address like `192.168.x.x` | STUN is missing. In a hand-written `voice.env`, add `export VOICE_STUN="stun.linphone.org:3478"`; in your own code, use `epCfg.uaConfig.stunServer.append("stun.linphone.org:3478")` |

## Thanks

- **[Linphone](https://www.linphone.org/)**: provides free SIP accounts and the client. sip.linphone.org carries only signaling; audio flows directly between the server and the phone (not yet verified by packet capture). If your usage is heavy, please consider supporting Linphone or running your own SIP server.
- **[Ringback](https://github.com/mohitbadwal/ringback)** (Apache 2.0): the upstream of this project.

## Credits

Research and design: Cora and South; implementation: Claude

## License

[AGPL-3.0-or-later](LICENSE). This project is derived from Ringback; the upstream code is licensed under Apache 2.0, and its original license and NOTICE are kept in [LICENSE-APACHE-2.0](LICENSE-APACHE-2.0) and [NOTICE](NOTICE). The pjproject dependency is GPL-2.0-or-later.
