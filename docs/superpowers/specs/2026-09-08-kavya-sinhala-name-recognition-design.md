# Kavya SmartPBX Sinhala Name Recognition Design

## Goal

Improve Direct SmartPBX Sinhala recognition of Sri Lankan customer names and
letter-by-letter spelling without changing English, Rime TTS, phone-number
validation, or the existing low-confidence confirmation policy.

## Root cause

The live Azure `si-LK` recognizer is biased for Sinhala numbers and Hatton Hills
room names, but its phrase list contains neither the existing Sri Lankan-name
vocabulary nor alphabet spelling forms. Azure also renders spoken English
letters as Sinhala words (for example, `C H A N Y A` as
`සී එච් ඒ එන් වයි ඒ`), while the deterministic name parser accepts ASCII
letters and English/NATO letter names only. The parser therefore cannot consume
the recognizer output reliably even when Azure heard the spelling.

## Design

1. Derive the Sinhala name hints from the existing English name-hint source so
   the two paths cannot silently drift.
2. Add those names, their space-separated spellings, A-Z, and the common Sinhala
   renderings of letter names as a Direct-SmartPBX-only extension of the existing
   `SI_STT_PHRASE_LIST`.
3. Add a pure word-boundary normalizer that rewrites only Sinhala letter-name
   tokens to ASCII letters.
4. Invoke that normalizer only at the Direct SmartPBX Sinhala dispatch boundary
   while `_capture_kind == "name"`, mirroring the existing phone-only Sinhala
   number normalizer.
5. Keep the existing Azure NBest confidence threshold, confirmation state,
   strict number validation, keypad fallback, English profile, and TTS paths
   byte-for-byte unchanged.

## Verification

- A real production-shaped transcript reconstructs `Chanya` deterministically.
- The Azure phrase list contains representative local names, spelled forms, and
  Sinhala letter names while remaining below Azure's 500-phrase guidance.
- Ordinary Sinhala conversation, phone capture, generic capture, and English
  SmartPBX dispatch remain unchanged.
- Python compilation and Git diff checks pass locally; behavioral pytest remains
  delegated to GitHub CI under the established Kavya workflow.
