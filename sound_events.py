"""Call-engine side of sound hearing (EXPERIMENTAL): stdlib only, off unless VOICE_SOUND_EVENTS=1.

Sends the turn's WAV (POST body, header X-Sound-Key, URL-quoted transcript in X-Sound-Text)
to a classifier endpoint you run yourself (VOICE_SOUND_URL) and expects a JSON list of
label strings back. The classifier itself is not included.
"""
import json, os, struct, urllib.parse, urllib.request

ENABLED = os.environ.get("VOICE_SOUND_EVENTS") == "1"
URL = os.environ.get("VOICE_SOUND_URL", "")
KEY = os.environ.get("VOICE_SOUND_KEY", "")
TIMEOUT = 3.0


def _wav_bytes(path):
    """pjsua may leave the WAV size fields stale; make them match the file."""
    with open(path, "rb") as f:
        data = bytearray(f.read())
    if len(data) <= 44 or data[:4] != b"RIFF":
        return b""
    at = data.find(b"data", 12)
    if at < 0:
        return b""
    struct.pack_into("<I", data, 4, len(data) - 8)
    struct.pack_into("<I", data, at + 4, len(data) - at - 8)
    return bytes(data)


def hear(path, text=""):
    """Return e.g. '[heard: breathing]', or '' on anything going wrong: a call must never fail here."""
    if not (ENABLED and URL and KEY):
        return ""
    try:
        body = _wav_bytes(path)
        if not body:
            return ""
        req = urllib.request.Request(URL, data=body, headers={"X-Sound-Key": KEY,
                                     "X-Sound-Text": urllib.parse.quote(text[:500])})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return "".join("[heard: %s]" % x for x in json.loads(r.read()))
    except Exception:
        return ""
