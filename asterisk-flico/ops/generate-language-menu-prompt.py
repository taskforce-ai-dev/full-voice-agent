#!/usr/bin/env python3
"""Generate the Asterisk language menu prompt as raw 8 kHz mu-law audio."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request


DEFAULT_TEXT = (
    "Please select your language. Press 1 for English. "
    "Press 2 for Tamil. Press 3 for Sinhala."
)


def load_env(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate custom/flico-language-menu.ulaw for Asterisk Read()."
    )
    parser.add_argument(
        "--env-file",
        default="/opt/flico/.env",
        help="Flico env file containing ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID.",
    )
    parser.add_argument(
        "--output",
        default="/opt/asterisk-flico/sounds/flico-language-menu.ulaw",
        help="Output path for raw 8 kHz mu-law audio.",
    )
    parser.add_argument(
        "--text",
        default=DEFAULT_TEXT,
        help="Prompt text to synthesize.",
    )
    args = parser.parse_args()

    env_values = load_env(pathlib.Path(args.env_file))
    api_key = os.getenv("ELEVENLABS_API_KEY") or env_values.get("ELEVENLABS_API_KEY")
    voice_id = os.getenv("ELEVENLABS_VOICE_ID") or env_values.get("ELEVENLABS_VOICE_ID")
    voice_speed = float(
        os.getenv("ELEVENLABS_VOICE_SPEED")
        or env_values.get("ELEVENLABS_VOICE_SPEED")
        or "1.0"
    )
    if not api_key or not voice_id:
        print("ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID are required", file=sys.stderr)
        return 1

    output = pathlib.Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    url = (
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
        "?output_format=ulaw_8000"
    )
    payload = {
        "text": args.text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.0,
            "use_speaker_boost": True,
            "speed": voice_speed,
        },
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            audio = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read(200).decode("utf-8", errors="replace")
        print(f"ElevenLabs returned {exc.code}: {body}", file=sys.stderr)
        return 1

    if len(audio) < 1600:
        print(f"Generated prompt is unexpectedly short: {len(audio)} bytes", file=sys.stderr)
        return 1

    output.write_bytes(audio)
    print(f"Wrote {output} ({len(audio)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
