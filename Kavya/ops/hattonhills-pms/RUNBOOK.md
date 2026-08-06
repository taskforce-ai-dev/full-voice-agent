# Hatton Hills PMS — runbook

Rebrands the Kavya booking database (`yanolja_pms` on **198.211.114.60**) from the
nine **Mosvold** room types to the five **Hatton Hills** room types.

Hatton Hills is an **invented single property** for client demos — a luxury
boutique eco retreat in Sri Lanka's central hill country. Every name and price
here is made up.

## What it changes

| id | code | name | USD/night | max pax | rooms |
|---|---|---|---|---|---|
| 1 | `HH-FES` | Forest Escape Suite | 700 | 2 | HH-101, HH-102 |
| 2 | `HH-ECO` | Eco Harmony Suite | 800 | 2 | HH-201, HH-202 |
| 3 | `HH-SVP` | Sunrise Vista Premium Suite | 950 | 2 | HH-301, HH-302 |
| 4 | `HH-MLX` | Mount Luxe Chalet | 1150 | 5 | HH-401, HH-402 |
| 5 | `HH-MON` | Mount Monarch Chalet | 1400 | 5 | HH-501 |

Types 7–10 (the ex-Sundara four) are **retired**: renamed to neutral
placeholders and `is_active=0`. They are kept, not deleted, so historical
reservations still resolve. Type 6 (`DUR`, Default Unmapped Room) is untouched.

`UPDATE` only — no `INSERT`, no `DELETE`, so all reservation foreign keys stay
valid.

Mount Monarch Chalet deliberately has **one** unit: it is the flagship, and the
knowledge base tells callers so. The other four have two units each so a single
demo booking does not exhaust a room type.

## Access

Needs **root** on `198.211.114.60`. The `dev` user cannot do this — it has no
MySQL grant and no passwordless sudo. The PMS REST API has no room-type write
endpoint, so this cannot be done over HTTP either.

## Run it

```bash
# 1. Confirm the column casing matches the SQL (expects snake_case)
sudo mysql yanolja_pms -e "SHOW COLUMNS FROM room_types; SHOW COLUMNS FROM rooms;"

# 2. Back up
sudo mysqldump yanolja_pms > /root/pms-pre-hattonhills-$(date +%Y%m%d-%H%M%S).sql

# 3. Apply
sudo mysql yanolja_pms < rename_to_hattonhills.sql

# 4. Verify in SQL
sudo mysql yanolja_pms -e "SELECT id,name,code,base_price,max_occupancy,is_active FROM room_types ORDER BY id;"
sudo mysql yanolja_pms -e "SELECT r.room_number,r.room_type_id,t.name FROM rooms r JOIN room_types t ON t.id=r.room_type_id ORDER BY r.room_number;"

# 5. Confirm no Mosvold string survives (both must return empty)
sudo mysql yanolja_pms -e "SELECT id,name,code FROM room_types WHERE code LIKE 'MV-%' OR code LIKE 'SU-%' OR name LIKE '%Deluxe%' OR name LIKE '%Founders%';"
sudo mysql yanolja_pms -e "SELECT room_number FROM rooms WHERE room_number LIKE 'MV-%' OR room_number LIKE 'SU-%';"
```

## Verify through the API (no root needed)

```bash
: "${YANOLJA_USERNAME:?set this protected environment variable first}"
: "${YANOLJA_PASSWORD:?set this protected environment variable first}"
TOKEN=$(printf '{"username":"%s","password":"%s"}' "$YANOLJA_USERNAME" "$YANOLJA_PASSWORD" | \
  curl -sS --fail -X POST https://yanolja.taskforceai.tech/api/auth/login \
    -H 'Content-Type: application/json' --data-binary @- | \
  python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
curl -s https://yanolja.taskforceai.tech/api/rooms -H "Authorization: Bearer $TOKEN" | python3 -m json.tool | head -40
```

Then verify through Kavya's own code path (this is the check that matters — it
exercises the same name-matching the agent uses):

```bash
cd /home/dev/full-voice-agent/Kavya
/home/dev/full-voice-agent/.venv/bin/python ops/hattonhills-pms/verify_live.py
```

## Keep in sync

The five room-type **names** are the single source of property identity — this
schema has no property column. They must match, byte-for-byte:

- `yanolja_service.ROOM_TYPES_BY_PROPERTY`
- `tools.ROOM_TYPES_BY_PROPERTY`
- `room_types.name` in this database

`yanolja_service._property_of()` returns `""` for any name it does not
recognise, and those rows are filtered out of availability. **A single typo
silently removes a room type from Kavya's inventory** — it will not error, the
room just stops existing.

Prices must match `DEMO_NIGHTLY_RATE_USD` in `yanolja_service.py`, or Kavya
quotes one figure on the call and the PMS folio shows another. `DEMO_RATES_ENABLED`
must stay `true` in Kavya's `.env`.

## Roll back

Uncomment the `REVERT` block at the bottom of `rename_to_hattonhills.sql` and run
it, or restore the mysqldump. Reverting the database alone is **not** the whole
rollback — also revert `knowledge_docs/hotel_info.txt`, `yanolja_service.py`,
`tools.py` and `server.py` (backups in `/home/dev/backups/kavya-kb/`).

## Known limitation — folio total

`reservations.total_amount` stays `0.00` on newly created bookings even with
`base_price` set. Kavya's create-reservation payload sends no total, and the PMS
does not appear to derive one at creation. This predates the Hatton Hills change
and is unresolved; it does not affect what Kavya quotes on the call.
