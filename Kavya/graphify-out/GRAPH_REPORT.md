# Graph Report - .  (2026-07-31)

## Corpus Check
- cluster-only mode — file stats not available

## Summary
- 799 nodes · 1198 edges · 42 communities (40 shown, 2 thin omitted)
- Extraction: 95% EXTRACTED · 5% INFERRED · 0% AMBIGUOUS · INFERRED: 58 edges (avg confidence: 0.78)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `e78f7da1`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 24|Community 24]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 27|Community 27]]
- [[_COMMUNITY_Community 28|Community 28]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 31|Community 31]]
- [[_COMMUNITY_Community 32|Community 32]]
- [[_COMMUNITY_Community 33|Community 33]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 35|Community 35]]
- [[_COMMUNITY_Community 36|Community 36]]

## God Nodes (most connected - your core abstractions)
1. `_Patcher` - 25 edges
2. `MediaStreamSession` - 23 edges
3. `ws_conversation()` - 23 edges
4. `execute_tool()` - 19 edges
5. `Change History` - 19 edges
6. `Change History` - 19 edges
7. `_request()` - 17 edges
8. `MediaStreamSession` - 14 edges
9. `_request()` - 14 edges
10. `CLAUDE.md` - 13 edges

## Surprising Connections (you probably didn't know these)
- `ws_conversation()` --calls--> `process_post_call_data()`  [INFERRED]
  server.py → post_call.py
- `lifespan()` --calls--> `initialize_kb()`  [INFERRED]
  media_stream_server.py → knowledge_base.py
- `lifespan()` --calls--> `prewarm()`  [INFERRED]
  media_stream_server.py → knowledge_base.py
- `ws_media_stream()` --calls--> `get_tools()`  [INFERRED]
  media_stream_server.py → tools.py
- `ws_conversation()` --calls--> `get_handover_tools()`  [INFERRED]
  server.py → tools.py

## Communities (42 total, 2 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.07
Nodes (60): create_guest(), create_reservation(), delete_reservation(), get_reservation(), get_session(), health(), init_bootstrap(), is_configured() (+52 more)

### Community 1 - "Community 1"
Cohesion: 0.06
Nodes (23): AzureSTTStream, _extract_sentences(), GoogleSTTStream, _make_stt(), MediaStreamSession, Split buffer on sentence boundaries; return (complete_sentences, remainder)., Streams mulaw 8 kHz audio to Google Cloud Speech-to-Text.      Runs the synchron, Streams audio to Azure Speech-to-Text — drop-in alternative to GoogleSTTStream. (+15 more)

### Community 2 - "Community 2"
Cohesion: 0.06
Nodes (51): add_document(), _cached_embed_query(), chunk_text(), _embed_texts(), _get_chroma_client(), _get_embedding_model(), initialize_kb(), prewarm() (+43 more)

### Community 3 - "Community 3"
Cohesion: 0.07
Nodes (25): _build_system_prompt(), _extract_sentences(), _get_azure_voice(), _get_client(), GoogleSTTStream, lifespan(), MediaStreamSession, media_stream_server.py — Multilingual Kavya server using Twilio Media Streams. (+17 more)

### Community 4 - "Community 4"
Cohesion: 0.05
Nodes (42): Architecture, Change History, CLAUDE.md, code:block1 (Full Voice agent/), code:block2 (C:/Users/mrdar/Downloads/ezeey-addon-extracted/ezeey-addon/), code:bash (# Install dependencies), code:block4 (Incoming call), code:block5 (Non-English call) (+34 more)

### Community 5 - "Community 5"
Cohesion: 0.05
Nodes (41): Architecture, Change History, code:block1 (Full Voice agent/), code:block2 (C:/Users/mrdar/Downloads/ezeey-addon-extracted/ezeey-addon/), code:bash (# Install dependencies), code:block4 (Incoming call), code:block5 (Non-English call), Commands (+33 more)

### Community 6 - "Community 6"
Cohesion: 0.08
Nodes (33): build_payload(), normalize_whatsapp(), handover.py -- Failsafe manager notification when a live handoff goes unanswered, Current UTC time as `2026-07-31T08:30:00Z` (matches the payload sample)., Assemble the n8n handover payload with both numbers normalised., POST the handover payload to n8n.      Returns `{"ok": bool, ...}`. Never raises, Normalise a spoken/dialled phone number to digits with a country code.      Retu, send_handover_notification() (+25 more)

### Community 7 - "Community 7"
Cohesion: 0.09
Nodes (35): _assert_no_money(), _clear_cache_and_env(), _Patcher, Offline unit tests for kpms_service business logic (Mosvold Boutique Hotels).  A, Reset module-level cache and pin the booking backend to one property.      kpms_, Helper that records POSTs to create_reservation/create_guest and serves     list, Recursively assert no currency figure appears in a caller-facing payload., With the backend pinned to Mosvold Villa, every Villa type is free.      The PMS (+27 more)

### Community 8 - "Community 8"
Cohesion: 0.10
Nodes (34): cancel_reservation(), _augment(), book(), _cache_get(), _cache_invalidate(), _cache_set(), cancel(), demo_rate_for() (+26 more)

### Community 9 - "Community 9"
Cohesion: 0.06
Nodes (24): pms(), Offline unit tests for yanolja_service in Hatton Hills SINGLE-PROPERTY mode.  Wh, A mismatch here silently removes a room type from Kavya's inventory., Guarantees the prefix/extends matching below can never be ambiguous., Returning None here would fail every tool call closed., is load-bearing: it is what keeps these rows out of availability., Regression guard for the subtle bug in the single-property collapse.      `resol, Must not silently pick a room — returns None or raises so Kavya asks. (+16 more)

### Community 10 - "Community 10"
Cohesion: 0.10
Nodes (31): _auth_block(), _cache_key(), cancel_booking(), check_availability(), close_session(), create_booking(), _get_cached(), get_session() (+23 more)

### Community 11 - "Community 11"
Cohesion: 0.08
Nodes (18): _dial_result(), Server-side tests for the unanswered-handoff failsafe.  Asserts the TwiML branch, Abandoned calls never clean themselves up - the dict must not grow forever., If the pre-transfer session died before stashing, still run the failsafe., Regression: a read-back-the-number rule made Kavya re-confirm a number the     g, Regression: on a live call Kavya opened with "I don't have your name from     ou, No number anywhere - a notification the manager can't act on is noise., `&` must be `&amp;` inside an XML attribute or Twilio rejects the TwiML. (+10 more)

### Community 12 - "Community 12"
Cohesion: 0.12
Nodes (18): _FakeResponse, _FakeSession, isolate(), n8n(), End-to-end test of the failsafe ConversationRelay session.  Drives a real `/ws/c, Hotel facts are noise when all Kavya needs is a name and a number., Sinhala/Arabic run on Media Streams; there is no transfer to fail there., Capture what would have been POSTed to the n8n handover webhook. (+10 more)

### Community 13 - "Community 13"
Cohesion: 0.11
Nodes (22): cancel_booking(), check_availability(), close_session(), create_booking(), get_session(), is_configured(), _parse_n8n_availability(), _parse_n8n_booking() (+14 more)

### Community 14 - "Community 14"
Cohesion: 0.14
Nodes (21): _clean_json_response(), extract_booking_details(), _extract_with_claude(), _extract_with_gemini(), _extract_with_openai(), _format_transcript(), _normalize_property_and_room(), _post_to_n8n() (+13 more)

### Community 15 - "Community 15"
Cohesion: 0.17
Nodes (18): Exception, create_guest(), create_reservation(), get_availability(), get_reservation(), _get_session(), is_configured(), list_reservations() (+10 more)

### Community 16 - "Community 16"
Cohesion: 0.12
Nodes (16): health(), _history_to_gemini(), _is_backchannel(), kb_reload(), server.py — Main FastAPI server for Hatton Hills Voice Agent (Kavya).  Hatton Hi, Convert OpenAI-format history to Gemini-native contents.      OpenAI format:, # NOTE: bookings go to the Yanolja PMS via booking_api -> yanolja_service ->, Return service health status. (+8 more)

### Community 17 - "Community 17"
Cohesion: 0.18
Nodes (13): _build_system_prompt(), _dashboard_call_started(), Stream an OpenAI response, handling tool use in a loop.      Sends text tokens t, Handle a Twilio ConversationRelay WebSocket session.      The ``lang`` query par, Build the system prompt for Claude, tailored to the caller's language.      The, _run_llm_streaming(), ws_conversation(), get_tools() (+5 more)

### Community 18 - "Community 18"
Cohesion: 0.14
Nodes (13): 1. Database, 2. App instance, 3. Seed (choose ONE), 4. PM2 + nginx, 5. Point Kavya at it (NOT the shared URL), 6. Smoke test (once creds exist), code:bash (mysql -e "CREATE DATABASE mosvold_pms CHARACTER SET utf8mb4;), code:bash (# separate checkout so the Treehouse instance is untouched) (+5 more)

### Community 19 - "Community 19"
Cohesion: 0.15
Nodes (12): get_handover_tools(), _matches_room_type(), normalise_property(), _property_required_error(), Claude tool definitions for the Hatton Hills voice agent (Kavya).  Defines the t, JSON error telling the model to establish the property first.      UNREACHABLE i, JSON error for a room type that does not belong to the given property., True when room_type names a room offered at property_name. (+4 more)

### Community 20 - "Community 20"
Cohesion: 0.17
Nodes (11): Access, code:bash (# 1. Confirm the column casing matches the SQL (expects snak), code:bash (TOKEN=$(curl -s -X POST https://yanolja.taskforceai.tech/api), code:bash (cd /home/dev/full-voice-agent/Kavya), Hatton Hills PMS — runbook, Keep in sync, Known limitation — folio total, Roll back (+3 more)

### Community 21 - "Community 21"
Cohesion: 0.23
Nodes (11): cancel_booking(), check_availability(), close_session(), create_booking(), get_session(), is_configured(), booking_api.py -- thin shim mapping legacy Kavya tool contract to yanolja_servic, Shared aiohttp session for non-PMS calls (post_call → n8n webhook).      Back-co (+3 more)

### Community 22 - "Community 22"
Cohesion: 0.26
Nodes (11): _announce_once(), _post(), Dashboard ingest client — fire-and-forget POSTs to agent-dashboard.  Reuses the, Emit a call.completed event with full transcript + extracted summary., Emit a call.transferred event when a call is handed off to a human., Log enabled/disabled status exactly once, on first public-API call., POST payload to the dashboard ingest endpoint. Never raises., Emit a call.started event for a new inbound call. (+3 more)

### Community 23 - "Community 23"
Cohesion: 0.18
Nodes (10): code:bash (/home/dev/full-voice-agent/.venv/bin/python ops/hattonhills-), code:bash (cp .env.example .env      # fill in API keys), Deploy, Full context for AI sessions, Kavya — Hatton Hills Voice Agent, Key files, Room types and rates, Run locally (+2 more)

### Community 24 - "Community 24"
Cohesion: 0.18
Nodes (11): _build_conversation_relay_twiml(), dial_result(), _get_twilio_client(), Twilio webhook for incoming phone calls.      By default (IVR_MENU_ENABLED unset, Handle the caller's DTMF language selection.      English (1) â†’ ConversationRe, Callback from <Dial action>. If the human answered â†’ hang up.     Otherwise, d, Build the <ConversationRelay> XML tag for the given language config.      `mode`, Record/merge handoff carry-over for a call, evicting the oldest entries. (+3 more)

### Community 25 - "Community 25"
Cohesion: 0.62
Nodes (9): check_config(), cmd_deploy(), cmd_logs(), cmd_setup(), cmd_status(), err(), info(), ok() (+1 more)

### Community 26 - "Community 26"
Cohesion: 0.25
Nodes (8): _get_anthropic_client(), _get_client(), _get_gemini_client(), lifespan(), Return the shared AsyncAnthropic client (for LLM_PROVIDER='claude')., Return the shared AsyncOpenAI client (for LLM_PROVIDER='openai')., Return the shared native Gemini client (for LLM_PROVIDER='gemini')., Startup / shutdown lifecycle for the FastAPI application.

### Community 27 - "Community 27"
Cohesion: 0.33
Nodes (6): _build_handoff_failsafe_prompt(), _format_handoff_transcript(), _notify_handover_fallback(), Render the last few turns of the pre-transfer call for the recovery prompt., System prompt for the recovery session after a human failed to answer.      Kavy, Notify the manager after a failsafe session ended without a tool call.      Runs

### Community 28 - "Community 28"
Cohesion: 0.33
Nodes (6): _is_tool_call_msg(), _is_tool_result_msg(), Check if a message is an orphaned tool result (Anthropic or OpenAI format)., Check if an assistant message contains tool calls (Anthropic or OpenAI format)., Keep conversation history within bounds.      Trims from the front (oldest messa, _trim_history()

### Community 29 - "Community 29"
Cohesion: 0.53
Nodes (5): call(), main(), names_of(), LIVE end-to-end: real tools.execute_tool -> booking_api -> yanolja_service -> LI, show()

### Community 30 - "Community 30"
Cohesion: 0.33
Nodes (5): buildCommand, framework, installCommand, outputDirectory, rewrites

### Community 31 - "Community 31"
Cohesion: 0.50
Nodes (4): ask(), main(), Drive the LIVE deployed Kavya over ConversationRelay WS, as Twilio would., Send one guest turn and collect Kavya's reply.      A turn can produce MORE than

### Community 32 - "Community 32"
Cohesion: 0.50
Nodes (4): Send a brief 'one moment please' filler if the LLM hasn't streamed     its first, Stream a Claude response via the Anthropic SDK, handling tool use.      Uses Ant, _run_llm_streaming_claude(), _slow_response_filler()

### Community 33 - "Community 33"
Cohesion: 0.67
Nodes (3): dump(), main(), Quick end-to-end smoke test for the Yanolja booking flow.  Runs inside the conta

### Community 34 - "Community 34"
Cohesion: 0.67
Nodes (3): d(), main(), Cancel the leftover Treehouse-era reservations that still hold Mosvold rooms.  W

## Knowledge Gaps
- **96 isolated node(s):** `buildCommand`, `outputDirectory`, `installCommand`, `framework`, `rewrites` (+91 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **2 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `execute_tool()` connect `Community 6` to `Community 32`, `Community 1`, `Community 2`, `Community 3`, `Community 16`, `Community 17`, `Community 19`?**
  _High betweenness centrality (0.047) - this node is a cross-community bridge._
- **Why does `ws_conversation()` connect `Community 17` to `Community 32`, `Community 1`, `Community 2`, `Community 14`, `Community 16`, `Community 19`, `Community 24`, `Community 26`, `Community 27`, `Community 28`?**
  _High betweenness centrality (0.042) - this node is a cross-community bridge._
- **Why does `send_handover_notification()` connect `Community 6` to `Community 27`?**
  _High betweenness centrality (0.035) - this node is a cross-community bridge._
- **Are the 6 inferred relationships involving `ws_conversation()` (e.g. with `process_post_call_data()` and `get_handover_tools()`) actually correct?**
  _`ws_conversation()` has 6 INFERRED edges - model-reasoned connections that need verification._
- **Are the 13 inferred relationships involving `execute_tool()` (e.g. with `._run_claude()` and `send_handover_notification()`) actually correct?**
  _`execute_tool()` has 13 INFERRED edges - model-reasoned connections that need verification._
- **What connects `post_call.py -- Post-call data extraction and n8n webhook integration.  Kavya is`, `Normalise the extracted property to the single canonical name.      Mutates and`, `Strip markdown code fences and whitespace from LLM JSON output.` to the rest of the system?**
  _326 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Community 0` be split into smaller, more focused modules?**
  _Cohesion score 0.06538461538461539 - nodes in this community are weakly interconnected._