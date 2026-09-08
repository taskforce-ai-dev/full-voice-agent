# Kavya SmartPBX Sinhala Name Recognition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Direct SmartPBX Sinhala name capture understand common Sri Lankan names and Azure's Sinhala-rendered English letter spellings.

**Architecture:** Extend the existing Azure `SI_STT_PHRASE_LIST` from the established English local-name source, then normalize Sinhala letter-name tokens at the same dispatch seam already used for Sinhala phone digits. The deterministic `assemble_spoken_name` parser remains the single assembler, and every non-Sinhala-name path stays untouched.

**Tech Stack:** Python 3.11, Azure Cognitive Services Speech SDK 1.51.1, pytest in GitHub CI.

## Global Constraints

- Direct SmartPBX Sinhala name capture only.
- Do not change English, Twilio, Rime/Gemini TTS, phone validation, endpointing timers, or confirmation policy.
- Do not log names, transcripts, or normalized spelling.
- Do not include `graphify-out` artifacts.

---

### Task 1: Specify name hints and Sinhala letter normalization

**Files:**
- Modify: `Kavya/tests/test_azure_stt_phrase_list.py`
- Modify: `Kavya/tests/test_sinhala_capture_dictation.py`

**Interfaces:**
- Consumes: `server.SMARTPBX_SI_NAME_STT_PHRASES`, `server._normalize_sinhala_spoken_letters`, and the existing Direct SmartPBX capture fixture.
- Produces: Regression contracts for phrase hints, deterministic Chanya reconstruction, and path isolation.

- [ ] **Step 1: Add RED phrase-list tests**

Assert representative local names (`Chanya`, `Shehani`, `Oshadi`), the
space-separated spelling `C H A N Y A`, Sinhala letter tokens (`සී`, `එච්`,
`වයි`), and a total phrase count no greater than 500.

- [ ] **Step 2: Add RED normalizer and dispatch tests**

Use the production-shaped input `උක්ත සාන්යා සී එච් ඒ. එන් වයි. ඒ.` and assert
that Direct SmartPBX Sinhala name capture dispatches
`උක්ත සාන්යා C H A. N Y. A.`, which `assemble_spoken_name` resolves to
`Chanya`. Assert that phone capture and ordinary conversation retain the
original Sinhala letter words.

- [ ] **Step 3: Demonstrate RED without local pytest**

Run a focused `python3 -c` import/assert probe. Expected result before the
implementation: `AttributeError` because
`_normalize_sinhala_spoken_letters` does not exist.

### Task 2: Implement the bounded Azure name vocabulary and dispatch normalizer

**Files:**
- Modify: `Kavya/server.py`

**Interfaces:**
- Produces: `_SI_SPOKEN_LETTER_WORDS`, `_SI_COMMON_NAME_PHRASES`,
  `_SI_SPELLED_NAME_PHRASES`, and
  `_normalize_sinhala_spoken_letters(text: str) -> str`.

- [ ] **Step 1: Derive common names from `_DEFAULT_EN_HINTS`**

Select the existing capitalized comma-separated entries so English output is
unchanged and there is one established local-name vocabulary.

- [ ] **Step 2: Extend the Direct SmartPBX Sinhala phrase list**

Add common names, spaced spellings, A-Z, and Sinhala spoken-letter keys through
ordered deduplication only when `direct_smartpbx_sinhala=True`. Preserve the
legacy Sinhala recognizer's existing phrase list.

- [ ] **Step 3: Add the pure normalizer**

Tokenize with `_SI_TOKEN_RE`, replace exact Sinhala spoken-letter tokens, and
preserve every separator and unrelated token verbatim.

- [ ] **Step 4: Wire only Direct Sinhala name capture**

At `_flush_transcript`, add an `elif` beside the existing phone-only branch:

```python
elif self._is_direct_smartpbx_sinhala() and self._capture_kind == "name":
    transcript = _normalize_sinhala_spoken_letters(transcript)
```

### Task 3: Verify and prepare review

**Files:**
- Verify: `Kavya/server.py`
- Verify: `Kavya/tests/test_azure_stt_phrase_list.py`
- Verify: `Kavya/tests/test_sinhala_capture_dictation.py`

**Interfaces:**
- Consumes: Task 1 regression contracts and Task 2 implementation.
- Produces: A reviewable Rakesh-branch change with no production deployment.

- [ ] **Step 1: Run the focused GREEN probe**

Assert that the production-shaped transcript normalizes and assembles to
`Chanya`.

- [ ] **Step 2: Run syntax and whitespace verification**

Run `python3 -m py_compile` for every touched Python file and `git diff --check`.

- [ ] **Step 3: Review the complete diff**

Confirm no changes to English, TTS, phone validation, endpointing timers,
secrets, or `graphify-out`.

- [ ] **Step 4: Push through Rakesh and let GitHub CI run pytest**

Only after Sol's review, create the narrow commit and PR. Do not merge or deploy
without explicit user approval.
