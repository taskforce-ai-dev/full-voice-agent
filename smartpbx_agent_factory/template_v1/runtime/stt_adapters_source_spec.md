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
