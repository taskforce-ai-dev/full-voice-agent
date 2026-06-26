"""Generate ~10s Sinhala TTS samples for every OpenAI gpt-4o-mini-tts voice.

Uses Fiona's production instructions so the comparison reflects the real agent.
Run from the Flico Agent dir; reads OPENAI_API_KEY from .env.
"""
import json
import os
import sys
import time
import urllib.request

VOICES = ["alloy", "ash", "ballad", "coral", "echo",
          "fable", "nova", "onyx", "sage", "shimmer", "verse"]

INSTRUCTIONS = (
    "You are Fiona, a warm and personable young Sri Lankan property consultant "
    "at a real estate agency. Speak in natural, lively conversational Sinhala "
    "with genuine warmth and enthusiasm — smile as you talk. Vary your pitch "
    "and pace naturally, sound like a real person chatting on the phone, not a "
    "robot reading text."
)

# ~10 seconds of natural Sinhala (Fiona greeting + a property-consult line)
TEXT = (
    "ආයුබෝවන්! ඔබ Rodrigo Realtors "
    "වෙත සම්බන්ධ වී "
    "සිටී. මම ෆියෝනා, "
    "ඔබගේ අතථ්‍ය "
    "පාරිභෝගික සේවා "
    "සහායිකාව. කොළඹ "
    "ප්‍රදේශයේ අලුත්ම "
    "නිවාස සහ මහල් "
    "නිවාස ගැන විස්තර "
    "දැනගන්න මම ඔබට අද "
    "උදව් කරන්නම්. "
    "කරුණාකර කියන්න, "
    "ඔබ සොයන්නේ කුමන "
    "ආකාරයේ දේපලක්ද?"
)


def load_key():
    with open(".env", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("OPENAI_API_KEY="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("OPENAI_API_KEY not found in .env")


def main():
    key = load_key()
    outdir = "voice_samples"
    os.makedirs(outdir, exist_ok=True)
    for voice in VOICES:
        body = json.dumps({
            "model": "gpt-4o-mini-tts",
            "input": TEXT,
            "voice": voice,
            "instructions": INSTRUCTIONS,
            "response_format": "mp3",
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.openai.com/v1/audio/speech",
            data=body,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
        )
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                audio = resp.read()
        except urllib.error.HTTPError as exc:
            print(f"{voice:9s} FAILED http={exc.code} {exc.read()[:200]!r}")
            continue
        path = os.path.join(outdir, f"sinhala_{voice}.mp3")
        with open(path, "wb") as fh:
            fh.write(audio)
        print(f"{voice:9s} ok  {len(audio):>7d}B  {time.time()-t0:4.1f}s")


if __name__ == "__main__":
    main()
