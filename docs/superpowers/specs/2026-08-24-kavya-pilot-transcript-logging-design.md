# Kavya Pilot Transcript Logging Design

## Goal

Restore real-time phrase-level diagnosis for controlled Kavya SmartPBX pilot
calls without weakening the default privacy-safe production logging contract.

## Design

Add `SMARTPBX_PILOT_TRANSCRIPT_LOGGING`, with an exact enabled value of `1` and
an off-by-default value of `0`. The SmartPBX Compose service explicitly forwards
the setting. Enabling it adds one dedicated log line for each finalized guest
turn dispatched to the agent and one for each exact phrase submitted to Kavya's
TTS path:

```text
smartpbx_pilot_transcript role=guest text='...'
smartpbx_pilot_transcript role=kavya text='...'
```

Text uses representation formatting so embedded newlines and control characters
cannot forge extra log records. The lines contain no caller or call identifier.
Google/Azure interim hypotheses remain privacy-safe, and existing operational
events remain unchanged.

## Operations

The flag is a temporary pilot diagnostic only. Enable it in the protected
`.env.smartpbx`, render Compose configuration, recreate the same pinned Kavya
image, and verify the enabled state without printing the environment file. Tail
the two dedicated roles locally. Restore `0` and recreate the same pinned image
when diagnosis is complete. Existing Docker log rotation remains the retention
bound; raw transcript logs must not be exported.

## Non-goals

- No STT, endpointing, number parsing, prompt, LLM, TTS, audio, or handover change.
- No raw interim hypotheses, caller identifiers, call identifiers, tool inputs,
  prompt text, KB context, credentials, or provider payloads.
- No change to Twilio or Flico paths.

## Verification

Regression coverage must prove that the default keeps guest and Kavya phrases
out of SmartPBX logs, that enabling the flag logs both finalized guest text and
TTS-submitted Kavya text, and that control characters are escaped. Deployment
coverage must prove the flag is explicitly allowlisted and documented with a
committed default of `0`.
