# Candidate runtime turn contract

This is a narrow, client-neutral extraction candidate from pinned Kavya revision
`6f6c2a3ae6f50e3ea84d293a24c37ef74808ec0e`. It is not an approved rendering
template and does not authorize an integration, deployment, or provider setup.

## Ownership model

- One `ContinuousRecognizer` is started for one call/language and receives every
  inbound audio frame. It reports ordered `RecognizerResult` callbacks rather
  than performing frame-scoped transcription.
- The callback is marshalled through `loop.call_soon_threadsafe`; only the event
  loop mutates endpointing, pending text, turn ownership, or output generation.
- Interims replace the endpoint candidate and reset the silence timer. Finals
  replace it and use the shorter final-grace timer. Result IDs are monotonic;
  duplicate or stale finals never dispatch a second turn.
- There is one active LLM/TTS task. A newer recognized utterance bars in: it
  invalidates endpointing, increments the output generation, cancels that task,
  and asks the media transport to discard queued audio. Every async boundary
  checks the owning generation before emitting media or completing a turn.
- A turn counts as completed only after `send_mark("conversation-turn")`
  returns. The pinned transport's mark waits for the paced media queue to drain,
  so this is a delivery barrier rather than a merely queued-media signal.
- Teardown closes callback admission before cancelling endpoint/turn work,
  clears media, then closes the recognizer. Late provider callbacks are ignored.

## Deliberate omissions

No tenant tools, booking/capture parsing, handover, prompt-derived capture mode,
provider SDK implementation, production configuration, or deployment behavior is
included. Those require separate approved interfaces and integration evidence.

## Pinned source evidence

- `Kavya/server.py:9359-9468`: STT worker callbacks are admitted onto the event
  loop and refused after the closing fence.
- `Kavya/server.py:9502-9635`: barge-in claims the current generation once,
  invalidates endpointing, then clears queued media.
- `Kavya/server.py:9703-9728`: delivered state follows the local transport mark.
- `Kavya/server.py:10835-11027`: endpoint tokens and an exactly-once active-turn
  guard prevent stale timer dispatches.
- `Kavya/smartpbx_transport.py:188-285`: `send_mark()` waits on the paced queue.
