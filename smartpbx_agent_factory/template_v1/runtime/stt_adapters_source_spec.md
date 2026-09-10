# STT adapter source specification

Status: candidate-only. This file documents a client-neutral extraction and does
not approve it for rendering or deployment.

The stable Kavya source is revision
`6f6c2a3ae6f50e3ea84d293a24c37ef74808ec0e`, `Kavya/server.py`.

| Source range | Required retained behavior |
| --- | --- |
| 575-582 | Provider/key configuration is established before calls, never read in an audio turn. |
| 1855-1863 | Language codes resolve to provider language configuration supplied at startup. |
| 6549-6881 | Google receives 8 kHz mu-law incrementally through a bounded worker queue and emits interim/final events. |
| 6884-6980 | Azure final metadata is bounded before it crosses into loop-owned handling. |
| 6983-7252 | Azure converts 8 kHz mu-law to 8 kHz, 16-bit mono PCM and Google/Azure share start, stop, and feed semantics. |
| 9359-9443 | Provider-thread callbacks only schedule event-loop work; endpointing and transcript ownership remain on the loop. |

The template must receive provider modules, credentials, client factories, loop,
and callbacks through startup configuration. It must not read an environment,
contain customer identity, decide endpointing, or contain business/knowledge
data.

## Shared runtime integration contract

The startup-injected `SmartPBXSTTAdapter` maps an exact generated language code
to its exact configured locale and provider. It exposes only
`start_recognizer(language, on_result)`, returning a call-local recognizer with
async `feed_audio` and `close`. The factory owns the `LoopEventBridge` until
that recognizer closes. Google and Azure events become `RecognizerResult` with
only Azure's bounded exact `result_id` as optional identity; offsets and
durations never become ordering inputs. Azure's startup future is awaited and
any start failure leaves the provider inactive. Azure JSON metadata is size
bounded before parsing.

## Fatal callback contract

The shared recognizer callback union includes `RecognizerFatal(reason)` in
addition to `RecognizerResult`. `reason` is the fixed bounded value
`provider_unavailable`; no SDK exception, cancellation detail, or provider
payload crosses this boundary. A call-local adapter emits at most one fatal on
its event loop after a Google worker failure or an unexpected Azure cancellation.
Recognizer close first fences the bridge, then stops the provider, suppressing
expected shutdown callbacks and every late fatal.
