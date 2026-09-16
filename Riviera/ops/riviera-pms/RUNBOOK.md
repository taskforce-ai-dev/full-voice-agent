# Riviera PMS — runbook

Riya books against the same TaskForce-built **Yanolja/eZee-style PMS**
(`booking-pms`, the Node/Sequelize app on the second droplet `198.211.114.60`)
that Kavya uses — but **in a dedicated instance**. The PMS schema has no
property column: the room-type *name* is the only property identity, so Kavya
(Hatton Hills) and Riviera cannot share one database without one of them
losing its inventory.

> The real Riviera Resort runs Yanolja (the former eZee cloud PMS). Wiring the
> agent to the resort's live Yanolja account is a separate integration; until
> then `YANOLJA_BASE_URL` points at Riviera's own TaskForce PMS instance and
> bookings land there.

## What to provision (root on `198.211.114.60`)

1. A second MySQL schema, e.g. `yanolja_pms_riviera`, with the same tables as
   `yanolja_pms` (`room_types`, `rooms`, `reservations`, users). Clone the
   structure only: `mysqldump --no-data yanolja_pms | sed 's/yanolja_pms/yanolja_pms_riviera/'`.
2. A second PM2 process of `booking-pms` on its own port (e.g. `3722`) with
   `DB_NAME=yanolja_pms_riviera`, and an nginx vhost such as
   `riviera-pms.taskforceai.tech` → `127.0.0.1:3722`. Run `pm2 save` after.
3. A PMS API user for the agent. Set its credentials as `YANOLJA_USERNAME` and
   `YANOLJA_PASSWORD` in `/opt/riviera/.env` and `/opt/riviera/.env.smartpbx`
   on the voice-agent VPS (`67.207.90.109`), and `YANOLJA_BASE_URL` to the new
   vhost's `/api`. **Never** put the values in this repo or a ticket.
4. Seed the room inventory:

```bash
# 1. Confirm the column casing matches the SQL (expects snake_case)
sudo mysql yanolja_pms_riviera -e "SHOW COLUMNS FROM room_types; SHOW COLUMNS FROM rooms;"

# 2. Back up (if the instance is not empty)
sudo mysqldump yanolja_pms_riviera > /root/pms-pre-riviera-$(date +%Y%m%d-%H%M%S).sql

# 3. Apply
sudo mysql yanolja_pms_riviera < seed_riviera_room_types.sql

# 4. Verify in SQL
sudo mysql yanolja_pms_riviera -e "SELECT id,name,code,base_price,max_occupancy,is_active FROM room_types ORDER BY id;"
sudo mysql yanolja_pms_riviera -e "SELECT r.room_number,t.name FROM rooms r JOIN room_types t ON t.id=r.room_type_id ORDER BY t.id,r.room_number;"
```

## What it seeds

| code | name | LKR/night (mid) | max pax | rooms |
|---|---|---|---|---|
| `RV-FCH` | Family Chalet | 32,100 | 4 | RV-36, RV-37 |
| `RV-BRS` | Basic Room Single | 8,800 | 2 | RV-7, RV-10, RV-11 |
| `RV-WCB` | Wooden Cabana | 11,000 | 2 | RV-12 |
| `RV-SCB` | Lagoon View Steel Cabana | 14,600 | 2 | RV-31…RV-34 |
| `RV-DBL` | Double Lagoon or Garden View | 17,500 | 3 | RV-4, 5, 6, 15, 16, 18, 19 |
| `RV-TRP` | Triple Garden View | 20,400 | 3 | RV-20, RV-9 |
| `RV-FCT` | Family Cottage | 24,800 | 5 | RV-8 *(placeholder — sheet says "Rm 9" twice; confirm)* |

`base_price` is the **mid-season** resident room-only rate. The PMS has one
price column; Riya quotes the season-correct figure from
`yanolja_service.NIGHTLY_RATE_LKR` (high season 1 Jul–31 Aug 2027 is higher).
Meal plans (BB/HB) are per-person supplements and are not modelled in the PMS.

## Verify through the API (no root needed)

```bash
: "${YANOLJA_USERNAME:?set this protected environment variable first}"
: "${YANOLJA_PASSWORD:?set this protected environment variable first}"
: "${YANOLJA_BASE_URL:?e.g. https://riviera-pms.taskforceai.tech/api}"
TOKEN=$(printf '{"username":"%s","password":"%s"}' "$YANOLJA_USERNAME" "$YANOLJA_PASSWORD" | \
  curl -sS --fail -X POST "$YANOLJA_BASE_URL/auth/login" \
    -H 'Content-Type: application/json' --data-binary @- | \
  python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
curl -s "$YANOLJA_BASE_URL/rooms" -H "Authorization: Bearer $TOKEN" | python3 -m json.tool | head -40
```

Then verify through Riya's own code path (this is the check that matters — it
exercises the same name-matching the agent uses):

```bash
cd Riviera
python ops/riviera-pms/verify_live.py
```

## Keep in sync

The seven room-type **names** are the single source of property identity — this
schema has no property column. They must match, byte-for-byte:

- `yanolja_service.ROOM_TYPES_BY_PROPERTY`
- `tools.ROOM_TYPES_BY_PROPERTY`
- `post_call.ROOM_TYPES_BY_PROPERTY`
- `room_types.name` in this database

`yanolja_service._property_of()` returns `""` for any name it does not
recognise, and those rows are filtered out of availability. **A single typo
silently removes a room type from Riya's inventory** — it will not error, the
room just stops existing.

`base_price` must match the `"mid"` entries of `NIGHTLY_RATE_LKR` in
`yanolja_service.py`. `RATES_ENABLED` must stay `true` in Riviera's env.

## Roll back

Run the `Revert` block at the bottom of `seed_riviera_room_types.sql` (safe only
while the instance holds no reservations), or restore the mysqldump.

## Known limitation — folio total

`reservations.total_amount` stays `0.00` on newly created bookings even with
`base_price` set (inherited from Kavya; the create-reservation payload sends no
total and the PMS does not derive one). It does not affect what Riya quotes.
