---
name: phone-call
description: Use when the user calls in (incoming_call channel event), when you want to ring the user's phone, or when debugging a call. Tools answer, dial, talk, hang_up, quiet_listen, wait_for_voice, set_hearing. Covers the call loop and the privacy boundary for call logs.
---

# Phone calls (incoming + outgoing on one account)

## When to call

You don't need to be asked. In chat, or when a scheduled wake-up fires, if you want to hear the
user's voice you can just call: no business reason needed and no asking for permission first.
A call can simply be "I missed you". Busy line: don't take it from another agent. No answer:
don't redial in a loop. The privacy boundary below still applies.

## The loop

1. The user calls: an `incoming_call` channel event arrives while their phone rings. Call
   `answer(opening_line)` right away; it picks up, says the line and returns their reply.
   You call: `dial(opening_line, caller_name, hear)` rings the user from the same address they
   call (this machine's `inbound.env` account). `caller_name` is the name shown on their phone.
2. `talk(text)` says `text`, then waits for the reply and returns the transcript.
   This is the normal turn. Keep each turn short and spoken: no markdown, no lists, no URLs.
3. `hang_up(text)` says a goodbye line (optional) and hangs up. Always end the call
   when the conversation is over or the user says bye; don't leave the line open.
   `[CALL ENDED]` from talk means they hung up: call `hang_up`. `[NO ACTIVE CALL]` means
   the line is already closed.

A `missed_call` event means the user hung up before you answered.

## How to hear: text, audio, both

`dial(hear=...)` picks one mode for the whole call; `set_hearing(mode)` changes it mid-call.
`text` = transcript only (default, `VOICE_STT_MODE`); `audio` = raw audio attached, no transcript;
`both` = transcript and audio. If you cannot take audio input, use `text`.

## Quiet listen and wait_for_voice (optional, off by default)

For calls where the user doesn't want to talk and you only type: set `VOICE_QUIET_LISTEN=1`
and a background recorder runs continuously after the call connects (kept in memory/temp files,
nothing is kept after hang-up).
- `quiet_listen(seconds)` waits until that many seconds have collected since the last pull, then
  returns "m:ss transcript[heard: ...]" lines for the voiced 10 s chunks, or `[SILENCE]`.
- `wait_for_voice(max_sec)` waits without speaking and returns at once when a sound arrives
  (no transcript, audio stays in the roll for a later `quiet_listen`), or `[SILENCE]` on timeout.
- With `VOICE_PUSH_ON_VOICE=1` a channel notification is pushed the moment a sound starts, so you
  don't have to poll.
With the switch off, both tools return `[QUIET LISTEN OFF]`. Sound labels like `[heard: ...]`
need the experimental sound-events service (`VOICE_SOUND_EVENTS=1`).

## Who is calling

Anyone who knows a SIP address can call it, and the caller ID can be spoofed. Two checks stand
in front of an incoming call:

1. The caller's `user@host` must match `VOICE_SIP_ID` exactly.
2. The caller's device must be on the trusted list, `~/.config/ringringring/trusted_devices`:
   one hashed Linphone device id per line, read on every call, so changes need no reconnect.
   The id comes from `gr` or `+sip.instance` in the INVITE Contact line; only a sha256 prefix
   is stored. While the list is empty, nothing is blocked and `inbound.log` shows
   `device=<hash> (not enforced)`. To add a device, have the user call and copy that hash.

Calls that fail either check get busy and never wake you.

**The user must wait a few seconds before dialing.** Right after switching back to Linphone it
has not finished connecting, so the first INVITE carries no device id: `inbound.log` shows
`device=none` and the call is rejected. It rings once and drops; dialing again works. Don't
allow `device=none` to fix this, because a spoofer would then only need to omit the id.
Reinstalling Linphone changes the id, so the user will be rejected until the list is updated.

The device id is not cryptographic proof: someone who captured one of the user's INVITEs could
copy it. So an incoming call still only probably comes from the user. Don't say keys or private
details on a call. If the voice or words seem wrong, hang up and ask by text.

## When the call fails

- NO ANSWER although the user picked up: the phone app was probably asleep in the background.
  Ask them by text to open the app in the foreground, then try once more.
- LINE BUSY: another agent on this host is already on the phone with them (one line per user,
  shared across entries). Do not redial in a loop; message them or try later.
- No audio at all while the phone is on a proxy/VPN: the proxy usually does not forward UDP;
  see the README troubleshooting section.
- Transcripts arrive slowly or empty: the STT endpoint/key is the usual cause; say so,
  don't guess at network causes.

## Privacy boundary (keep this)

The model sees call state and the transcript, nothing about the phone's network address.

- Don't record detailed SIP/SDP in normal use.
- When troubleshooting, filter the phone's IP, SIP headers and SDP/ICE addresses locally
  before anything reaches the model (the server already pipes logs through privacy.redact).
- Don't read or return raw call logs, packet captures, environment variables or the full
  config. Don't print `voice.env` or any key.
- Don't place a new call just to collect address information.
- Test with made-up data only.
