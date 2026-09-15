#!/usr/bin/env python3
"""Render Riviera's static SmartPBX language-menu asset (smartpbx_language_menu.ulaw).

The Dialog SmartPBX line plays a pre-rendered μ-law prompt before any STT or
LLM exists for the call (see SMARTPBX_RUNBOOK.md "Static SmartPBX language
menu"). Kavya's committed asset announces only English (1) and Sinhala (2);
Riviera also offers Tamil (3), so the asset must be regenerated with three
segments and committed in the same change as any prompt-wording edit.

Wire contract enforced by smartpbx_session._load_smartpbx_language_menu_audio:
  * 8 kHz G.711 μ-law, total length a multiple of 160 bytes (20 ms frames)
  * exactly 2,400 leading bytes of 0xFF (15 frames / 300 ms of digital silence)
  * the first material frame must not itself be all-0xFF
  * at most 512 frames (81,920 bytes, 10.24 s) in total

Segments (in order):
  1. English  — canonical Riya voice (ElevenLabs, RIVIERA_EN_ELEVENLABS_VOICE_ID,
                eleven_flash_v2_5, ulaw_8000):
                "Welcome to Riviera Resort. For English, press 1."
  2. Sinhala  — Gemini TTS (SMARTPBX_SINHALA_GEMINI_TTS_MODEL / _VOICE, the same
                model/voice the Sinhala call path uses), 24 kHz PCM -> μ-law:
                "සිංහල සඳහා, 2 ඔබන්න."
  3. Tamil    — ElevenLabs multilingual (ELEVENLABS_VOICE_ID, eleven_multilingual_v2,
                ulaw_8000): "தமிழுக்கு, மூன்றை அழுத்தவும்."

Run it OFF the production host with the protected credentials exported in the
shell (never paste them into the repo, a ticket or a log):

    ELEVENLABS_API_KEY=... RIVIERA_EN_ELEVENLABS_VOICE_ID=... ELEVENLABS_VOICE_ID=... \
    GEMINI_API_KEY=... python3 scripts/generate_smartpbx_language_menu.py

Then listen to the result (`sox -t ul -r 8000 smartpbx_language_menu.ulaw out.wav`),
commit it, and ship it in a new image — the runtime validates the asset at call
start and refuses to play a malformed one.
"""
from __future__ import annotations

import audioop
import base64
import json
import os
import sys
import urllib.request
from pathlib import Path

FRAME = 160
PREROLL = b"\xff" * (15 * FRAME)
GAP = b"\xff" * (10 * FRAME)          # 200 ms of silence between segments
MAX_FRAMES = 512
OUT = Path(__file__).resolve().parents[1] / "smartpbx_language_menu.ulaw"

EN_TEXT = "Welcome to Riviera Resort. For English, press 1."
SI_TEXT = "සිංහල සඳහා, 2 ඔබන්න."
TA_TEXT = "தமிழுக்கு, மூன்றை அழுத்தவும்."


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"{name} is required (export it in the shell; never commit it)")
    return value


def _elevenlabs_ulaw(text: str, voice_id: str, model_id: str) -> bytes:
    url = (
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        "?output_format=ulaw_8000"
    )
    body = json.dumps({
        "text": text,
        "model_id": model_id,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75,
                            "style": 0.0, "use_speaker_boost": True},
    }).encode()
    req = urllib.request.Request(url, data=body, headers={
        "xi-api-key": _env("ELEVENLABS_API_KEY"),
        "Content-Type": "application/json",
        "Accept": "audio/basic",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def _gemini_ulaw(text: str) -> bytes:
    model = os.environ.get("SMARTPBX_SINHALA_GEMINI_TTS_MODEL", "gemini-3.1-flash-tts-preview")
    voice = os.environ.get("SMARTPBX_SINHALA_GEMINI_TTS_VOICE", "Vindemiatrix")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        f"?key={_env('GEMINI_API_KEY')}"
    )
    body = json.dumps({
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}},
        },
    }).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.load(resp)
    part = payload["candidates"][0]["content"]["parts"][0]["inlineData"]
    pcm = base64.b64decode(part["data"])
    # Gemini returns 24 kHz 16-bit mono PCM; downsample to 8 kHz then μ-law encode.
    pcm8k, _ = audioop.ratecv(pcm, 2, 1, 24000, 8000, None)
    return audioop.lin2ulaw(pcm8k, 2)


def _pad(chunk: bytes) -> bytes:
    remainder = len(chunk) % FRAME
    return chunk if not remainder else chunk + b"\xff" * (FRAME - remainder)


def main() -> int:
    en = _pad(_elevenlabs_ulaw(EN_TEXT, _env("RIVIERA_EN_ELEVENLABS_VOICE_ID"), "eleven_flash_v2_5"))
    si = _pad(_gemini_ulaw(SI_TEXT))
    ta = _pad(_elevenlabs_ulaw(TA_TEXT, _env("ELEVENLABS_VOICE_ID"), "eleven_multilingual_v2"))
    if en[:FRAME] == b"\xff" * FRAME:
        sys.exit("English segment starts with a silent frame; trim leading silence before use")
    asset = PREROLL + en + GAP + si + GAP + ta
    frames = len(asset) // FRAME
    if frames > MAX_FRAMES:
        sys.exit(f"asset is {frames} frames; the runtime allows at most {MAX_FRAMES} — shorten the prompts")
    OUT.write_bytes(asset)
    print(f"wrote {OUT} ({len(asset)} bytes, {frames} frames, ~{frames * 0.02:.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
