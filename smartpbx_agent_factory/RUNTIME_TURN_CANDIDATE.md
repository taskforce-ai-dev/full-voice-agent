# Candidate runtime turn contract

This is a narrow, client-neutral extraction candidate from pinned Kavya revision
`0f83ac662a34657f06d490a46dc1b773bfe021e7`, including the reviewed V07
compatibility-marker boundary. It is not an approved rendering
template and does not authorize an integration, deployment, or provider setup.

## Ownership model

- One `ContinuousRecognizer` is started for one call/language and receives every
  inbound audio frame. It reports ordered `RecognizerResult` callbacks rather
  than performing frame-scoped transcription.
- The callback is marshalled through `loop.call_soon_threadsafe`; only the event
  loop mutates endpointing, pending text, turn ownership, or output generation.
- A recognizer may instead send the bounded, transcript-free `RecognizerFatal`
  event. The event loop admits it once, closes further callback admission,
  cancels/fences the active turn and queued media, stops endpointing, closes the
  recognizer, and completes the session's terminal failure signal with the
  generic `stt_fatal` reason. Expected recognizer close is fenced first and is
  never converted into a fatal event.
- Interims replace the endpoint candidate and reset the silence timer. Finals
  replace it and use the shorter final-grace timer. Duplicate callback identity
  is suppressed only within a small bounded window.
  Provider IDs are optional and never used as a global ordering clock, so a
  reset/reordered provider ID cannot discard fresh caller speech.
- Barge-in is classified only while the current generation is pre-audio or
  audible. It requires material text (default 12 stripped characters) and, once
  audio has begun, waits the default 0.6-second audible debounce. If the adapter
  can identify self-audio, echoed text is ignored. Short/debounced speech stays
  admitted in the endpoint buffer; it does not cancel the active reply.
- There is one active LLM/TTS task. A qualifying recognized utterance bars in: it
  invalidates endpointing, increments the output generation, cancels that task,
  and asks the media transport to discard queued audio. Every async boundary
  checks the owning generation before emitting media or completing a turn.
- A turn counts as completed only after `send_mark("conversation-turn")`
  returns. The pinned transport's mark waits for the paced media queue to drain,
  so this is a delivery barrier rather than a merely queued-media signal.
- Teardown closes callback admission before cancelling endpoint/turn work,
  clears media, then closes the recognizer. Late provider callbacks are ignored.
- Streaming LLM text is provisional for the active generation. Each complete
  `ProvisionalSentence` starts TTS promptly, but only `TerminalCommit` may send
  the delivery mark, increment the completed-turn counter, or write the
  response to committed history. A `GenerationFence` first cancels/awaits the
  current sentence task, clears queued transport audio, and discards the
  provisional response before the adapter's single recovery retry can speak.
  A truncated, aborted, or stale stream therefore has no committed history or
  surviving queued media, while its first sentence is not delayed for whole
  stream completion.
- The streaming event names and fields intentionally match the separately
  reviewed LLM lane: generation-scoped `ProvisionalSentence`,
  `ThinkingProgress`, `TerminalCommit`, `GenerationFence`, and
  `RecoveryBoundary`. Integration must bind both lanes to one shared event-type
  module; this partial candidate does not choose that module boundary.

## Deliberate omissions

No tenant tools, booking/capture parsing, handover, prompt-derived capture mode,
provider SDK implementation, production configuration, or deployment behavior is
included. Those require separate approved interfaces and integration evidence.

## Pinned source evidence

- `Kavya/server.py:9359-9468`: STT worker callbacks are admitted onto the event
  loop and refused after the closing fence.
- `Kavya/server.py:6591-6746` and `7017-7200`: exhausted Google stream restart
  and unexpected Azure cancellation signal fatal ownership once; requested STT
  shutdown does not report a fatal callback.
- `Kavya/server.py:9502-9635`: barge-in claims the current generation once,
  invalidates endpointing, then clears queued media.
- `Kavya/server.py:9703-9728`: delivered state follows the local transport mark.
- `Kavya/server.py:10835-11027`: endpoint tokens and an exactly-once active-turn
  guard prevent stale timer dispatches.
- `Kavya/smartpbx_transport.py:188-285`: `send_mark()` waits on the paced queue.
