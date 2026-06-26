# Flico / Rodrigo Realtors — Rental KB Replacement (2026-06-23)

## Goal
Replace the Flico voice agent's knowledge base (`Flico Agent/knowledge_docs/flico_info.txt`)
with a fresh **rental** inventory of 49 Colombo properties (sourced from a LankaPropertyWeb
export of 50, less P04 which is already rented). The current KB is a *sales* catalog for
Rodrigo Realtors; this flips the business model to **rentals** while keeping the agency
brand (Rodrigo Realtors) and voice persona (Fiona).

## Decisions (confirmed with user)
- **Business model:** rewrite intro / areas-covered / next-steps as a Colombo rental agency
  (monthly rent, deposits, advance, minimum lease, viewings).
- **Photo URLs:** omitted from the spoken KB (not speakable, pollute RAG). Consultant sends
  photos via WhatsApp/email on follow-up.
- **Branding:** unchanged — Rodrigo Realtors / Fiona.
- **POR (Price on Request):** stated as "rent available on request, a consultant will share it".

## Document structure
1. Header + intro (rental focus)
2. Areas covered — Colombo zones 1–10 with neighbourhood character (C1–C10 context)
3. APARTMENTS FOR RENT (33 listings)
4. HOUSES FOR RENT (8 listings)
5. COMMERCIAL & OFFICE SPACE FOR RENT (8 listings)
6. NEXT STEPS (rental follow-up)

## Per-listing chunk (retrieval-optimized, one self-contained paragraph)
Building/development + Colombo zone (with area name), type, beds/baths, floor area
(+ land perches where given), rent in words + figures (incl. US$ where shown) or
"rent on request", furnishing, availability, lease terms (deposit / advance / minimum
lease / parking where given), key features. No P-codes, no URLs.

## Data handling notes
- **P04 (Cinnamon Life):** availability = "Rented" → excluded (49 listings remain).
- **P01:** title Rs 170,000 vs price field Rs 150,000 → use structured field (Rs 150,000).
  Size "750,000 sq.ft / 60 perches" implausible for an office → render as 60 perches of
  land, drop the sq-ft figure.
- **P03:** rent is "Rs 15,000 per day" (daily/short-term) → rendered as per-day.
- **P08:** title mentions "$1800" but price field is POR → rendered as rent-on-request.
- Truncated source titles → clean descriptive names from building + location fields.
- Lookup tables (by zone/type/price with P-codes) not copied verbatim — same info lives
  in each paragraph; the P-code index is noise for voice RAG.

## Verification
After writing, an independent subagent adversarially audits every listing paragraph
against the source export (count = 49, correct beds/baths/size/rent/furnishing/zone,
POR phrasing, no URLs/P-codes leaked).

## Deployment
KB goes live on the Flico container (`flico.taskforceai.tech`, port 8003) and ChromaDB
re-indexes — same flow as the recent Hatton Hills / Kavya KB updates. Confirm before
pushing to production.
