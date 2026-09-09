#!/usr/bin/env python3
"""Generate the IAAC/Vidya Dialog SmartPBX language-menu audio.

`smartpbx_session._load_smartpbx_language_menu_audio()` loads and STRICTLY
validates `smartpbx_language_menu.ulaw` at the start of every Dialog call. The
cloned file still speaks the Kavya / Hatton Hills prompt, so it MUST be replaced
with an IAAC-branded English + Sinhala menu before go-live, e.g.:

    "Welcome to IAAC, the International Airline and Aviation College.
     For English, press one.  සිංහල සඳහා, දෙක ඔබන්න."   (press 2 for Sinhala)

This script takes ANY source recording (a human recording, or a clip you
synthesised with ElevenLabs / Gemini / OpenAI TTS — your choice) and encodes it
to the exact wire format the loader accepts:

  * G.711 mu-law ("ulaw"), 8000 Hz, mono
  * a 2400-byte pure-silence (0xFF) pre-roll at the front
  * total length a whole number of 160-byte (20 ms) frames
  * no longer than 512 frames (~10.24 s)
  * the first frame after the pre-roll must NOT be silence

Usage (run on the VPS or the dev rig; ffmpeg is required and is already in the
agent image):

    python3 ops/generate_language_menu.py --from-audio menu_source.wav
    python3 ops/generate_language_menu.py --from-audio menu.mp3 --out smartpbx_language_menu.ulaw

It converts the source with ffmpeg, mu-law encodes it, adds the pre-roll, pads
to a frame boundary, VALIDATES it against the loader's rules, and only then
writes the output. If validation fails it tells you why and writes nothing.

Note: `audioop` is stdlib on Python 3.11 (the agent image). On 3.13+ install
`audioop-lts` first (`pip install audioop-lts`).
"""
from __future__ import annotations

import argparse
import audioop
import subprocess
import sys
from pathlib import Path

# These MUST match smartpbx_session.py exactly.
_ULAW_FRAME_BYTES = 160          # 20 ms of 8 kHz mu-law
_PREROLL_BYTES = 2_400           # _LANGUAGE_MENU_PREROLL_BYTES
_MAX_FRAMES = 512                # _MAX_LANGUAGE_MENU_FRAMES
_SILENCE = b"\xff"               # mu-law digital silence
_SAMPLE_RATE = 8000


def _decode_to_pcm16_8k_mono(src: Path) -> bytes:
    """Use ffmpeg to turn any input into raw 8 kHz mono signed-16-bit PCM."""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-ac", "1", "-ar", str(_SAMPLE_RATE),
        "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1",
    ]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True)
    except FileNotFoundError:
        sys.exit("ffmpeg not found. Install ffmpeg (it is already in the agent image).")
    except subprocess.CalledProcessError as exc:
        sys.exit(f"ffmpeg failed:\n{exc.stderr.decode('utf-8', 'ignore')}")
    if not result.stdout:
        sys.exit("ffmpeg produced no audio (empty/invalid source file).")
    return result.stdout


def _encode(pcm16_mono_8k: bytes) -> bytes:
    """PCM16 -> mu-law, prepend pre-roll, pad to a whole number of frames."""
    ulaw = audioop.lin2ulaw(pcm16_mono_8k, 2)
    body = _SILENCE * _PREROLL_BYTES + ulaw
    remainder = len(body) % _ULAW_FRAME_BYTES
    if remainder:
        body += _SILENCE * (_ULAW_FRAME_BYTES - remainder)
    return body


def _validate(audio: bytes) -> None:
    """Reproduce the loader's checks so a bad file is caught here, not on a call."""
    first_material = audio[_PREROLL_BYTES:_PREROLL_BYTES + _ULAW_FRAME_BYTES]
    problems = []
    if len(audio) <= _PREROLL_BYTES:
        problems.append("shorter than the required pre-roll")
    if len(audio) % _ULAW_FRAME_BYTES:
        problems.append(f"length {len(audio)} is not a multiple of {_ULAW_FRAME_BYTES}")
    if len(audio) > _MAX_FRAMES * _ULAW_FRAME_BYTES:
        secs = _MAX_FRAMES * _ULAW_FRAME_BYTES / _SAMPLE_RATE
        problems.append(f"longer than the {secs:.2f}s ({_MAX_FRAMES} frames) maximum -- trim the source")
    if audio[:_PREROLL_BYTES] != _SILENCE * _PREROLL_BYTES:
        problems.append("pre-roll is not pure silence")
    if first_material == _SILENCE * _ULAW_FRAME_BYTES:
        problems.append("first frame after the pre-roll is silent (add speech sooner / trim leading silence)")
    if problems:
        sys.exit("Generated audio is INVALID and was not written:\n  - " + "\n  - ".join(problems))


def main() -> None:
    ap = argparse.ArgumentParser(description="Encode the IAAC SmartPBX language-menu prompt.")
    ap.add_argument("--from-audio", required=True, metavar="FILE",
                    help="Source recording (wav/mp3/m4a/etc.) of the bilingual menu prompt.")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "smartpbx_language_menu.ulaw"),
                    help="Output .ulaw path (default: the agent's smartpbx_language_menu.ulaw).")
    args = ap.parse_args()

    src = Path(args.from_audio)
    if not src.is_file():
        sys.exit(f"Source not found: {src}")

    audio = _encode(_decode_to_pcm16_8k_mono(src))
    _validate(audio)
    out = Path(args.out)
    out.write_bytes(audio)
    frames = len(audio) // _ULAW_FRAME_BYTES
    secs = len(audio) / _SAMPLE_RATE
    print(f"OK  wrote {out}  ({len(audio)} bytes, {frames} frames, {secs:.2f}s) -- validation passed")


if __name__ == "__main__":
    main()
