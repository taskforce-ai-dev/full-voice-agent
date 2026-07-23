# Mosvold PMS — isolated demo instance runbook

Goal: give the Mosvold Kavya demo its own booking backend, with **zero impact on
the live Treehouse Kavya** that runs against the shared `yanolja_pms` database.

**Why a separate instance:** the existing Yanolja PMS
(`yanolja.taskforceai.tech` → `127.0.0.1:5000`, database `yanolja_pms`) feeds the
production Treehouse agent, and the PMS schema has **no property/tenant column** —
room-type *name* is the only identity. Seeding Mosvold rooms into it would make the
live Treehouse agent see 14 room types and could quote Mosvold rooms to real
Treehouse callers. So Mosvold gets its own DB + process + vhost.

All steps below need **root** on `198.211.114.60` (the `dev` user is walled off from
PM2/nginx/MySQL and cannot do these). The seed artifacts referenced live in this
folder: `mosvoldData.ts`, `seed_room_types.sql`.

## 1. Database
```bash
mysql -e "CREATE DATABASE mosvold_pms CHARACTER SET utf8mb4;"
# create a dedicated user or reuse the PMS user with access scoped to mosvold_pms
```

## 2. App instance
```bash
# separate checkout so the Treehouse instance is untouched
cp -r /var/www/yanolja-cunt-eezy /var/www/mosvold-pms
cd /var/www/mosvold-pms/backend
# .env for THIS instance only:
#   DB_NAME=mosvold_pms
#   PORT=5001                 # not 5000 (that's Treehouse)
#   (copy JWT/secret config from the Treehouse .env or generate fresh)
```

## 3. Seed (choose ONE)
- **App-native:** drop `mosvoldData.ts` into `backend/src/seed/`, and in the
  instance bootstrap call `seedMosvoldData()` instead of `seedDemoData()`. Rebuild
  (`npm run build`) and start once so Sequelize creates tables and seeds.
- **Direct SQL:** after the app has created the schema once, run
  `mysql mosvold_pms < seed_room_types.sql` (read the column-casing note at the top
  of that file first).

Verify: `SELECT name, code, maxOccupancy FROM room_types;` → the nine Mosvold names
plus `Default Unmapped Room`, and no Treehouse names.

## 4. PM2 + nginx
```bash
pm2 start ecosystem.config.js --name mosvold-pms   # confirm it binds :5001
pm2 save
# nginx: new server block, e.g. mosvold-pms.taskforceai.tech -> 127.0.0.1:5001
# certbot for that hostname; DNS A record -> 198.211.114.60
```

## 5. Point Kavya at it (NOT the shared URL)
The Mosvold Kavya deployment must set, in its runtime `.env`:
```
YANOLJA_BASE_URL=https://mosvold-pms.taskforceai.tech/api
YANOLJA_USERNAME=<mosvold instance admin>
YANOLJA_PASSWORD=<...>
```
`yanolja_client.py` defaults `YANOLJA_BASE_URL` to the **shared** Treehouse URL, so
this override is mandatory — without it the Mosvold agent would hit Treehouse's PMS.
Do not bake Mosvold credentials into the repo.

## 6. Smoke test (once creds exist)
From the Mosvold agent env, run a two-property round trip and confirm:
- `check_availability(property="Mosvold Villa", …)` returns Villa rooms only
- `check_availability(property="Sundara by Mosvold", …)` returns Sundara rooms only
- a create/cancel booking round-trip succeeds at each property
- a cross-property room request (e.g. "Beach Villa" at "Mosvold Villa") is refused

## Known limitation (carried from the review)
Property identity is inferred from room-type **name**, not a stored id. If an
operator later renames a room and drops its distinguishing words (e.g. Sundara's
"Deluxe Double Room with Sea View" → bare "Deluxe Double Room"), it will be
classified as Mosvold Villa and bookings can land at the wrong property. Keep the
nine names exactly as seeded. A durable fix (bind property to room-type id/code)
is tracked separately in the code review follow-ups.
