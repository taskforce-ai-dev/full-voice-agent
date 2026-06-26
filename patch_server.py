"""Patch server.py to capture create_booking results and pass to post_call."""
import sys

with open(sys.argv[1], "r") as f:
    content = f.read()

changes = 0

# ---- PATCH 1: Init booking_details variable ----
old_cr_init = "    full_transcript: list[dict[str, str]] = []\n    call_start_time = datetime.now().isoformat()"

new_cr_init = "    full_transcript: list[dict[str, str]] = []\n    booking_details: dict[str, Any] | None = None\n    call_start_time = datetime.now().isoformat()"

count = content.count(old_cr_init)
print(f"Found full_transcript init {count} time(s)")
content = content.replace(old_cr_init, new_cr_init)
changes += count

# ---- PATCH 2: Capture create_booking result in ConversationRelay handler ----
old_tool_log_cr = '                logger.info("Tool \'%s\' result: %s", tb["name"], result_str[:200])\n\n            conversation_history.append({"role": "user", "content": tool_results})'

new_tool_log_cr = '                logger.info("Tool \'%s\' result: %s", tb["name"], result_str[:200])\n                if tb["name"] == "create_booking":\n                    try:\n                        _bk = json.loads(result_str)\n                        if _bk.get("success"):\n                            booking_details = _bk\n                    except Exception:\n                        pass\n\n            conversation_history.append({"role": "user", "content": tool_results})'

if old_tool_log_cr in content:
    content = content.replace(old_tool_log_cr, new_tool_log_cr)
    changes += 1
    print("Patched ConversationRelay tool capture")
else:
    print("WARNING: Could not find ConversationRelay tool log pattern")

# ---- PATCH 3: Capture create_booking result in MediaStreams handler ----
old_tool_log_ms = '                    logger.info("Tool \'%s\' → %s", tb["name"], result_str[:200])\n\n                self.history.append({"role": "user", "content": tool_results})'

new_tool_log_ms = '                    logger.info("Tool \'%s\' → %s", tb["name"], result_str[:200])\n                    if tb["name"] == "create_booking":\n                        try:\n                            _bk = json.loads(result_str)\n                            if _bk.get("success"):\n                                self._booking_details = _bk\n                        except Exception:\n                            pass\n\n                self.history.append({"role": "user", "content": tool_results})'

if old_tool_log_ms in content:
    content = content.replace(old_tool_log_ms, new_tool_log_ms)
    changes += 1
    print("Patched MediaStreams tool capture")
else:
    print("WARNING: Could not find MediaStreams tool log pattern")

# ---- PATCH 4: Pass booking_details in ConversationRelay post_call ----
old_pc = """            asyncio.create_task(
                process_post_call_data(
                    call_sid=call_sid,
                    lang=lang,
                    caller_phone=caller_phone,
                    full_transcript=full_transcript,
                    call_start_time=call_start_time,
                    call_end_time=call_end_time,
                    llm_provider=LLM_PROVIDER,
                    anthropic_client=anthropic_client,
                    openai_client=openai_client,
                    gemini_client=gemini_client,
                    model=MODEL,
                )
            )"""

new_pc = """            asyncio.create_task(
                process_post_call_data(
                    call_sid=call_sid,
                    lang=lang,
                    caller_phone=caller_phone,
                    full_transcript=full_transcript,
                    call_start_time=call_start_time,
                    call_end_time=call_end_time,
                    llm_provider=LLM_PROVIDER,
                    anthropic_client=anthropic_client,
                    openai_client=openai_client,
                    gemini_client=gemini_client,
                    model=MODEL,
                    booking_details=booking_details,
                )
            )"""

cr_count = content.count(old_pc)
print(f"Found process_post_call_data calls: {cr_count}")
content = content.replace(old_pc, new_pc)
changes += cr_count

# ---- PATCH 5: Pass booking_details in MediaStreams post_call ----
old_pc_ms = """                    process_post_call_data(
                        call_sid=self.call_sid,
                        lang=self.lang,
                        caller_phone=self.caller_phone,
                        full_transcript=self.full_transcript,
                        call_start_time=self.call_start_time,
                        call_end_time=call_end_time,
                        llm_provider=LLM_PROVIDER,
                        anthropic_client=self.anthropic_client,
                        openai_client=self.client,
                        gemini_client=self.gemini_client,
                        model=MODEL,
                    )"""

new_pc_ms = """                    process_post_call_data(
                        call_sid=self.call_sid,
                        lang=self.lang,
                        caller_phone=self.caller_phone,
                        full_transcript=self.full_transcript,
                        call_start_time=self.call_start_time,
                        call_end_time=call_end_time,
                        llm_provider=LLM_PROVIDER,
                        anthropic_client=self.anthropic_client,
                        openai_client=self.client,
                        gemini_client=self.gemini_client,
                        model=MODEL,
                        booking_details=getattr(self, "_booking_details", None),
                    )"""

if old_pc_ms in content:
    content = content.replace(old_pc_ms, new_pc_ms)
    changes += 1
    print("Patched MediaStreams post_call call")
else:
    print("WARNING: Could not find MediaStreams post_call pattern")

with open(sys.argv[1], "w") as f:
    f.write(content)

print(f"OK: server.py patched ({changes} changes)")
