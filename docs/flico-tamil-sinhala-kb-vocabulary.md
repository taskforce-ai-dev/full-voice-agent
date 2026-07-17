# Tamil / Sinhala filter vocabulary — RESEARCHED, NOT SHIPPED

**Status: blocked on a native-speaker review. Do not ship as-is.**

`kb/query_parser.py` is English-only, so Tamil and Sinhala utterances extract no
filters. Since `n_results=None` landed, that is far less bad than it sounds: an
unfiltered query returns the **complete** inventory, so nothing is hidden from the
LLM — it just does the filtering itself. Measured end-to-end at 17/17 including
in-language Tamil and Sinhala scenarios (`evals/answer_eval.py`).

**So this work is a SCALABILITY fix, not an accuracy fix.** At ~12 listings,
handing the LLM everything works. At the real ~49-row portfolio it becomes slow
and expensive per turn and eventually forces truncation back, at which point
Tamil/Sinhala silently start losing listings again. That is when this becomes
urgent.

## Why it is not shipped

Shipping unverified vocabulary would make a **currently-working path worse**.
Today TA/SI get a complete context and answer correctly. Add native-script
filters and any wrong mapping starts *excluding* listings the caller qualifies
for — trading a measured-good state for a possibly-broken one. The failure is
also silent and confident, which is the worst kind on a live customer call.

Fable's own flags: `high` = confident in spelling and meaning. `medium` = meaning
certain, spelling/STT rendering may vary (fails safe — a wrong spelling never
matches, so worst case is today's behaviour). `low` = plausible but unverified,
**do not ship**.

## What to do before shipping any of this

1. **Log a week of raw pre-KB transcripts per language.** This is the only way to
   resolve the MEDIUM/LOW spelling flags without a native speaker, and the only
   way to settle the digit question below. Log the exact STT output before it
   reaches the KB.
2. Have a Sinhala and a Tamil speaker audit the tables — the glosses are there so
   meaning can be checked separately from spelling.
3. Implement behind the exhaustive grammar sweep: add every entry as a
   `(fragment, expected_contribution)` to `tests/test_exhaustive_parser.py`
   **including the traps as decoys that must extract NOTHING**, and require the
   sweep green before deploy.

## Three structural facts that break a naive port

A direct translation of the English regex approach fails silently:

1. **Sinhala numerals FOLLOW the noun**: "two bedrooms" = කාමර දෙකක් ("rooms
   two-INDEF"). Tamil precedes, like English.
2. **Comparators are postposed on a dative suffix in both languages**: "under 3
   lakh" = TA மூன்று லட்சத்துக்கு கீழ் / SI ලක්ෂ තුනට අඩු — the amount comes
   first and the under/over word last, the reverse of English.
3. **`\b` does not work on Indic script** (verified empirically): consonants are
   `\w` but vowel signs and viramas are not, so `\b` fires *mid-word* (false
   anchors) and a trailing `\b` *rejects* legitimate inflections — `\bනිවසb`
   fails on නිවසක් ("a house"). Both languages agglutinate case suffixes.
   The safe primitive is a same-script negative lookbehind
   (`(?<![඀-෿])` / `(?<![஀-௿])`) as the leading anchor, stem
   matching with **no** trailing anchor for nouns, and a whitelisted suffix set
   for numbers.

## Substring traps — the "island → land" equivalents, worst first

| trap | wrong reading | fix |
|---|---|---|
| SI කාමර inside නාන කාමර | two **bathrooms** → `bedrooms=2` | lookbehind `(?<!නාන\s)` |
| SI පහ (5) prefix of පහසුකම් "facilities" | "room facilities" → `bedrooms=5` | number-suffix whitelist |
| SI හත (7) prefix of හතර (4) | "four" → 7 | longest-first alternation |
| SI හය (6) inside දහය (10) | "ten" → 6 | leading same-script lookbehind |
| SI නව (combining 9) also = "new" | නව නිවාස "new houses" → 9 | only match full නවය |
| SI එක (1) also colloquial "the" | "හවුස් එක" → count 1 | only adjacent to a counted noun |
| TA ஒரு = "one" **and** "a/an" | "கொழும்புல ஒரு வீடு" ("a house in Colombo") → **zone 1** | never feed ஒரு to the zone extractor |
| TA பத்து (10) inside இருபத்து (20) | 25 → 10 | leading lookbehind |
| TA கடை "shop" prefix of கடைசி "last" | "finally…" → `commercial` | `கடை(?!சி)` |
| TA காணி "plot" prefix of காணிக்கை "offering" | donation talk → `land` | `காணி(?!க்கை)` |
| TA நில- root: நிலா moon, நிலை state, நிலையம் station | anything → `land` | match நிலம/நிலத்த exactly, never the bare root |
| TA ஆறு = 6 **and** "river" | riverside talk → 6 | numeral must be adjacent to a bedroom/scale word |
| TA குறைவாக "less" (ceiling) vs குறைந்தது "at least" (floor), shared root குறை | **budget ceiling read as a floor** | match full forms; குறைவ- only postposed, குறைந்தது only preposed |
| TA அதிக "more" (floor) prefix of அதிகபட்சம் "maximum" (ceiling) | **max read as min** | `அதிக(?!பட்ச)` |
| TA மேல் "above" vs மேலும் "furthermore" | connective → rent floor | `மேல(?!ும்)` |
| TA வரை "up to" also "until/as far as" | "கொழும்பு வரை" → rent cap | require amount+scale immediately before |
| SI වර්ග අඩි / පර්චස් **precede** the figure | "වර්ග අඩි 1000ට අඩු" → `max_rent=1000` | Sinhala needs a **lookbehind** not-money guard; the English trailing lookahead is on the wrong side |
| SI නිවස vs නිවාසය | *not* a substring — vowel sign ා intervenes (codepoint-verified) | safe as-is, but **never strip vowel marks** in a normalizer or the penthouse→house bug returns |

## Digit reality

Not contractually specified by Google, and unverified for si-LK/ta-IN. Design
defensively; support digits **and** words everywhere.

- Neither script uses its own digit glyphs in practice (Tamil ௧௨௩ / Sinhala
  illakkam are archaic). When STT emits digits they are **ASCII**. No native
  numeral glyph handling is ever needed.
- Google's Indic ITN typically renders zone-like numbers and large amounts as
  ASCII digits, and `alternative_language_codes: en-US` pushes further toward
  digits and Latin tokens. Expect "කොළඹ 7", "2 பெட்ரூம்", "300000" to be common.
- But small cardinal counts inside fluent native speech frequently survive as
  **words** (කාමර දෙකක්, இரண்டு படுக்கையறை).

Per field: **zones** — digits primary, words belt-and-braces. **bedrooms** —
number words are *load-bearing* (1–4 are spoken naturally). **money** — the native
**scale words** are load-bearing (ලක්ෂ / லட்சம் may carry either digit or word).

## Vocabulary

### Colombo + areas

```python
_NATIVE_AREA_TO_ZONE = {
    # --- Sinhala ---
    "කොම්පඤ්ඤ වීදිය": 2,   # "Kompanna Veediya" = Slave Island (lit. Company Street)
                            # MEDIUM: meaning certain, spacing/spelling may vary
    "කොල්ලුපිටිය": 3,       # Kollupitiya — HIGH
    "වැල්ලවත්ත": 6,         # Wellawatte (inflects: වැල්ලවත්තේ "in W.") — HIGH
    "කුරුඳුවත්ත": 7,         # "Kurunduwatta" = Cinnamon Gardens — HIGH
    "බොරැල්ල": 8,           # Borella — HIGH
    # --- Tamil ---
    "கொள்ளுப்பிட்டி": 3,    # Kollupitiya — HIGH
    "வெள்ளவத்தை": 6,        # Wellawatte (inflects: வெள்ளவத்தையில்) — HIGH
    "பொரளை": 8,             # Borella — MEDIUM (variant பொறளை exists)
    "கொம்பனித்தெரு": 2,     # "Company Street" = Slave Island — MEDIUM (traditional;
                            # younger callers say the English name)
    "கறுவாத்தோட்டம்": 7,    # calqued "cinnamon garden" — MEDIUM (callers usually
                            # say "கொழும்பு ஏழு" instead)
}
# "Colombo N" — match the STEM so inflections still hit:
#   SI කොළඹ (HIGH; කොළඹට "to Colombo")
#   TA கொழும்ப (HIGH; covers கொழும்பில் / spoken கொழும்புல — the "-la" in
#              "Colombo 7 la")
```

**Omitted, needs a speaker:** Union Place (no verifiable native name; the en-US
alternate emits "Union Place" in Latin, which the existing map already catches);
Havelock Town (pure transliteration, several plausible STT spellings — LOW);
Latin romanizations like "kolamba"/"veedu" (not what either STT channel produces;
adding them risks new English collisions for no measured gain).

### Property types

```python
_NATIVE_TYPES = {
    "apartment": (
        "அடுக்குமாடி",   # TA "multi-storey (dwelling)" — HIGH
        "அபார்ட்",       # TA stem of the English loan — MEDIUM
        "මහල් නිවාස",    # SI codebase-vetted; stem so නිවාසය/නිවාසයක් hit — HIGH
        "තට්ටු නිවාස",   # SI "storeyed house / flat" — MEDIUM-HIGH
        "ඇපාට්",         # SI stem of the English loan — MEDIUM
    ),
    "house": (
        "வீடு",          # TA "house/home" — HIGH (see caveat)
        "வீட்ட",         # TA inflected stem (வீட்டில், வீட்டுக்கு) — HIGH
        "නිවස",          # SI vetted; stem covers නිවසක්/නිවසේ — HIGH
        "නිවෙස",         # SI spelling variant — MEDIUM
        "ගෙයක්",         # SI "a house" (colloquial, very common) — HIGH
    ),
    "commercial": (
        "வணிக",          # TA "commercial" (adj) — HIGH
        "வர்த்தக",       # TA "trade/commercial" (adj) — HIGH
        "அலுவலக",        # TA stem of "office" — HIGH
        "கடை",           # TA "shop" — HIGH, REQUIRES (?!சி) guard
        "වාණිජ",         # SI vetted (වාණිජ දේපළ) — HIGH
        "කාර්යාල",       # SI stem of "office" — HIGH
        "ඔෆිස්",         # SI English loan — MEDIUM (ඕෆිස් variant)
        "කඩය",           # SI "shop" — MEDIUM, low retrieval value
    ),
    "land": (
        "நிலம",          # TA stem of நிலம் "land" — HIGH
        "நிலத்த",        # TA inflected stem — HIGH
        "காணி",          # TA SL-Tamil "plot of land", very common — HIGH,
                         # guard (?!க்கை)
        "ඉඩම",           # SI vetted; stem covers ඉඩමක්/ඉඩම් — HIGH
    ),
}
```

**Deliberate exclusions — these are the dangerous ones:**

- TA **பிளாட்** — transliterates BOTH "flat" and "plot". Either mapping misfiles
  the other group. Exclude entirely.
- SI **ගෙදර** — means "home", not "a house to rent": "ගෙදර යන්න ඕන" = "I want to
  go home". Including it turns small talk into `property_type=house`.
- TA **இல்லம்** — formal "home", same generic problem, wrong register.
- Caveat, not exclusion: TA வீடு is also generic "dwelling", so a Tamil caller
  wanting any home may say it. Mirrors English callers saying "house" loosely.

### Bedroom / bathroom

```python
_NATIVE_BEDROOM = (
    "படுக்கையறை",   # TA "bedroom" (sandhi form) — HIGH
    "படுக்கை அறை",  # TA same, two words — HIGH
    "பெட்ரூம்",     # TA English loan — HIGH (dominant in Tanglish)
    "නිදන කාමර",    # SI vetted; stem — HIGH
    "බෙඩ්රූම්",     # SI English loan — MEDIUM (also බෙඩ් රූම්)
    "කාමර",         # SI "room(s)" alone: "කාමර දෙකක්" conventionally MEANS two
                    # bedrooms — MEDIUM; MUST carry the (?<!නාන\s) guard
)
_NATIVE_BATHROOM = (   # DECOYS, not filters — the schema has no bathroom filter
    "குளியலறை",     # TA "bathroom" — HIGH (no overlap with படுக்கையறை)
    "பாத்ரூம்",     # TA English loan — MEDIUM
    "නාන කාමර",     # SI vetted — HIGH. CONTAINS කාමර: "නාන කාමර දෙකක්" (two
                    # bathrooms) must never parse as bedrooms=2
    "බාත්රූම්",     # SI English loan — LOW-MEDIUM
)
```

### Numbers 1–10

```python
# SINHALA — numeral FOLLOWS the noun (කාමර දෙකක් = "two rooms"). These are STEMS;
# real speech agglutinates a suffix from a closed set: -ක් (indef), -ක්වත්
# ("at least"!), -ට/-ටත් (dative), -යි, -ේ. Match stem + WHITELISTED suffix +
# not-followed-by-Sinhala. A bare \b or free tail silently misfires.
_SI_NUM = {
    "එක": 1,    # HIGH as a numeral, but also colloquial definite marker
                # ("හවුස් එක" = "the house") — MEDIUM in extraction position
    "දෙක": 2,   # HIGH (දෙකක්, දෙකේ, දෙකට)
    "තුන": 3,   # HIGH (prefix of තුනී "thin" — suffix whitelist blocks it)
    "හතර": 4,   # HIGH — MUST be tried before හත (7), which is its prefix
    "පහ": 5,    # HIGH — prefix of පහසුකම්/පහළ/පහත; whitelist makes it safe
    "හය": 6,    # HIGH — substring of දහය (10); needs a leading anchor
    "හත": 7,    # HIGH (see හතර ordering)
    "අට": 8,    # HIGH
    "නවය": 9,   # HIGH — use the FULL form; combining නව- also means "NEW"
    "දහය": 10,  # HIGH
}
# Combining forms that FUSE into the money word: දෙලක්ෂ "2 lakh", තුන්ලක්ෂ
# "3 lakh", පන්ලක්ෂ "5 lakh" — HIGH for දෙ=2, තුන්=3, පන්=5. Others MEDIUM.

# TAMIL — numeral PRECEDES the noun, like English. Formal forms are what ta-IN
# usually emits; colloquial spellings appear when it transcribes casual speech.
_TA_NUM = {
    "ஒன்று": 1,   # HIGH. NB "ஒரு" = 1 but ALSO "a/an" — never feed it to zone
    "இரண்டு": 2,  # HIGH (colloquial ரெண்டு — MEDIUM whether STT normalizes)
    "மூன்று": 3,  # HIGH (colloquial மூணு)
    "நான்கு": 4,  # HIGH (colloquial நாலு)
    "ஐந்து": 5,   # HIGH (colloquial அஞ்சு)
    "ஆறு": 6,     # HIGH — homograph of "river"; safe only adjacent to a
                  # bedroom/scale word (river inflects to ஆற்ற-)
    "ஏழு": 7,     # HIGH (ஏழில் "in seven" for zones)
    "எட்டு": 8,   # HIGH
    "ஒன்பது": 9,  # HIGH
    "பத்து": 10,  # HIGH — substring of இருபத்து (20); needs a leading anchor
}
```

### Comparison / budget

Direction is what to audit here — two confusable pairs per language flip ceiling
vs floor.

```python
_TA_COMPARE = {
    # ceiling, EXCLUSIVE ("under/below/less than")
    "க்கு கீழ்":     "amount-DAT + 'below' — மூன்று லட்சத்துக்கு கீழ் — HIGH",
    "விட குறைவாக":  "'less than' (postposed) — HIGH",
    "குறைவான":      "'lesser/cheaper' (postposed after amount) — HIGH",
    # ceiling, INCLUSIVE ("up to/max/budget")
    "வரை":          "'up to' (postposed) — HIGH; also 'until/as far as' —"
                    " require amount+scale immediately before",
    "அதிகபட்சம்":    "'maximum' (preposed) — HIGH",
    "பட்ஜெட்":       "'budget' (loan) — MEDIUM spelling",
    # floor ("at least/minimum/over")
    "குறைந்தது":     "'at least' (preposed) — HIGH",
    "குறைந்தபட்சம்": "'minimum' (preposed) — HIGH",
    "க்கு மேல்":     "amount-DAT + 'above' — HIGH; guard (?!ும்) vs மேலும்",
    "விட அதிகமாக":  "'more than' (postposed) — HIGH; guard (?!பட்ச)",
    # range
    "முதல் ... வரை": "'from X to Y' — HIGH structure",
    "இடையில்":       "'between' — HIGH word; inflects both amounts"
                    " (Xக்கும் Yக்கும் இடையில்) — MEDIUM to implement",
}

_SI_COMPARE = {
    # ceiling, EXCLUSIVE
    "ට අඩු":       "amount-DAT + 'less' — ලක්ෂ තුනට අඩු — HIGH; bare අඩු also"
                   " = 'cheap' — require an amount before it",
    "ට වඩා අඩු":   "'less than' — HIGH",
    # ceiling, INCLUSIVE
    "දක්වා":       "'up to' (postposed) — HIGH",
    "උපරිම":       "'maximum' (preposed) — HIGH",
    "බජට්":        "'budget' (loan; often බජට් එක) — MEDIUM",
    # floor
    "ට වඩා වැඩි":  "'more than' — HIGH",
    "ට වැඩි":      "'more than' (short) — HIGH",
    "අවම වශයෙන්":  "'at least' — HIGH",
    "අවම":         "'minimum' (preposed) — HIGH",
    "-වත් suffix":  "'at least' FUSED onto the number: කාමර දෙකක්වත් = 'at least"
                   " two rooms' — HIGH. This is how min_bedrooms is actually"
                   " said in Sinhala.",
    # range
    "සිට ... දක්වා": "'from X to Y' — HIGH structure",
    "අතර":          "'between' — HIGH word, MEDIUM construction",
}
# EXCLUDED: SI අයවැය "budget" — that is a GOVERNMENT budget, wrong register.
```

### Money scale

```python
_NATIVE_SCALE = {
    # Tamil — amount precedes scale; scale takes the dative when a comparator
    # follows (லட்சம் -> லட்சத்துக்கு). Match stems.
    "லட்ச":     100_000,     # "lakh" — HIGH
    "ஆயிர":     1_000,       # "thousand" — HIGH
    "கோடி":     10_000_000,  # "crore" — HIGH (sale prices)
    "மில்லியன":  1_000_000,   # "million" loan — LOW-MEDIUM (uncommon in Tamil;
                             # lakhs/crores dominate)
    # Sinhala — SCALE PRECEDES the number: ලක්ෂ තුනක් = "lakh three" = 300,000.
    "ලක්ෂ":     100_000,     # "lakh" — HIGH
    "දහස":      1_000,       # "thousand" — HIGH
    "දාහ":      1_000,       # colloquial "dāha(k)" — HIGH (long ā; distinct
                             # from the දහ- "10-" combining form)
    "මිලියන":   1_000_000,   # "million" loan — MEDIUM-HIGH (genuinely common)
    "කෝටි":     10_000_000,  # "crore" — MEDIUM
}
_NATIVE_RUPEES = ("ரூபாய்", "ரூபா", "රුපියල්")   # TA / SL-Tamil short / SI — HIGH
```

### Occupancy decoys — must extract NOTHING

```python
_OCCUPANCY_DECOYS = (
    "பேர்",        # TA person-counter: "நாங்கள் நாலு பேர்" = "we are four PEOPLE"
    "நபர்",        # TA "person(s)"
    "குடும்பம்",   # TA "family" (inflects: குடும்பத்துக்கு)
    "எங்களுக்கு",  # TA "for us"
    "දෙනෙක්",     # SI person-classifier: "අපි හතර දෙනෙක්" = "we are four PEOPLE"
    "දෙනා",       # SI same classifier, definite (හතරදෙනා fused)
    "පවුල",       # SI "family" (පවුලේ)
    "අපිට",       # SI "for us" (also අපට)
)
```

**Safe by construction, not just by blocklist:** person-classifier phrases put the
number next to the *person* word, never next to a bedroom noun. If the Sinhala
bedroom pattern is strictly NOUN-then-NUMBER and the Tamil pattern is
NUMBER-then-BEDROOM-NOUN, occupancy phrases have no path into the filters.

Sizes are likewise safe in Tamil (the unit follows the figure: "1000 சதுர அடி",
so a money regex requiring a scale/comparator right after the figure never
fires) — but **not in Sinhala**, where the unit *precedes* the figure
("වර්ග අඩි 1000ට අඩු" = under 1000 sqft). An English-style trailing not-money
lookahead cannot see it; Sinhala needs a lookbehind on අඩි / පර්චස්.

## Still untestable without a speaker or call recordings

- Exact STT spellings of English-loan transliterations (ඇපාට්මන්ට්, பெட்ரூம்,
  බජට්, ඔෆිස්).
- The Havelock Town / Union Place native renderings.
- Whether colloquial Tamil numerals (ரெண்டு/மூணு/நாலு/அஞ்சு) are emitted verbatim
  or normalized to the formal forms.
- Sinhala combining numerals beyond දෙ- / තුන් / පන්-.
