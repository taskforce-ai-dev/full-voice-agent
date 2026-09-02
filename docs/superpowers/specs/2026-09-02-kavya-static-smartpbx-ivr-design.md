# Kavya Static SmartPBX IVR Design

## Goal

Play the complete English/Sinhala language menu promptly on every SmartPBX call without a live TTS request or per-call IVR cost.

## Scope

- SmartPBX only. Twilio, Flico, and the post-selection English and Sinhala agent streams remain unchanged.
- Preserve the spoken menu: “For English, press 1.” followed by “සිංහල සඳහා, 2 ඔබන්න.”
- Preserve DTMF interruption, invalid-selection replay, the eight-second language-selection timeout, and fail-closed Gemini credential admission.
- Production uses no separate transport preroll. The static menu contains exactly 300 ms of leading 8 kHz G.711 μ-law silence.

## Design

Generate the two approved voices once, convert them to the existing SmartPBX wire format, prepend fifteen 20 ms silence frames, and commit one immutable `smartpbx_language_menu.ulaw` asset. Load and validate the asset once per process. A menu task sends those bytes through the existing `SmartPBXMediaTransport`, then waits on its normal delivery barrier. A DTMF selection continues to call `clear_audio()`, which retires the generation and discards the unheard remainder.

The asset contract is deliberately strict: it must exist, be nonempty, be aligned to 160-byte frames, and begin with exactly 2,400 bytes of μ-law silence. A missing or malformed asset terminates admission before partial IVR playback. Docker’s explicit runtime allowlist must copy the asset.

## Operations

Deploy the image through the existing guarded SmartPBX image workflow. In the same reversible maintenance transaction, change only `SMARTPBX_STARTUP_PREROLL_MS` from `100` to `0`, recreate only `kavya-smartpbx`, and verify handover, image identity, Flico/legacy isolation, and the static-menu runtime contract. Retain the previous image and protected environment for rollback.

## Acceptance

- A cold-start call begins with 300 ms of silence and then the complete word “For”.
- No ElevenLabs or Gemini TTS request is issued for the language menu.
- Pressing 1 or 2 clears the remaining menu and activates exactly one profile.
- Invalid selection replays the same local asset.
- Dynamic English and Sinhala responses retain their current providers and behavior.
