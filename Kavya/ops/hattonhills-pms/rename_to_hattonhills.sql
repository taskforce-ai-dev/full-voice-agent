-- rename_to_hattonhills.sql
-- Rebrand the Kavya PMS from the NINE Mosvold room types to the FIVE
-- Hatton Hills room types, in place, using UPDATE only.
--
-- Hatton Hills is an INVENTED single property (a luxury boutique eco retreat in
-- Sri Lanka's central hill country) used for client demonstrations. Everything
-- below — names, codes, prices — is made up. It is not a real rate card.
--
-- WHY UPDATE-ONLY: there are no INSERTs and no DELETEs in this migration. The
-- existing room_types rows are renamed and the existing rooms rows are
-- reassigned, so every reservation foreign key stays valid and historical
-- bookings keep resolving. Nothing is orphaned.
--
-- ROOM NAMES ARE LOAD-BEARING. This schema has NO property column: the room-type
-- NAME is the single source of property identity for the voice agent. These five
-- strings must match Kavya's yanolja_service.ROOM_TYPES_BY_PROPERTY and
-- tools.ROOM_TYPES_BY_PROPERTY BYTE-FOR-BYTE. Do not reword, abbreviate,
-- re-case or "tidy" them — yanolja_service._property_of() returns "" for any
-- name it does not recognise, and rows returning "" are filtered out of
-- availability entirely. A single typo silently removes a room type from the
-- agent's inventory.
--
-- COLUMN CASING: the Sequelize models declare camelCase attributes (basePrice,
-- maxOccupancy, isActive, roomNumber, roomTypeId) but the physical MySQL columns
-- are snake_case: base_price / max_occupancy / is_active / created_at /
-- updated_at, and in `rooms`: room_number / room_type_id / housekeeping_status.
-- This file uses snake_case. CONFIRM BEFORE RUNNING:
--   SHOW COLUMNS FROM room_types;
--   SHOW COLUMNS FROM rooms;
-- and adjust the identifiers if this instance differs.
--
-- PRICES must stay in sync with DEMO_NIGHTLY_RATE_USD in yanolja_service.py, or
-- Kavya quotes one figure on the call and the PMS folio shows another. Kavya
-- must also keep DEMO_RATES_ENABLED=true in its .env for rates to be quoted.
--
-- Currency: US dollars, per room per night, half board, taxes included.
--
-- VERIFIED STARTING STATE (queried via the REST API before writing this):
--   id=1  MV-DDR base=700.00 max=2 'Deluxe Double Room'                   room MV-101
--   id=2  MV-DTR base=700.00 max=2 'Deluxe Twin Room'                     room MV-102
--   id=3  MV-FAM base=820.00 max=4 'Family Suite'                         room MV-201
--   id=4  MV-FND base=900.00 max=2 'Founders Suite'                       room MV-202
--   id=5  SU-BV  base=880.00 max=4 'Beach Villa'                          room SU-905
--   id=7  SU-DDG base=700.00 max=2 'Deluxe Double Room with Garden View'  room SU-901
--   id=8  SU-DDS base=760.00 max=2 'Deluxe Double Room with Sea View'     room SU-902
--   id=9  SU-DTS base=760.00 max=2 'Deluxe Twin Room with Sea View'       room SU-903
--   id=10 SU-FVP base=900.00 max=4 'Family Villa with Pool'               room SU-904
--   id=6  DUR 'Default Unmapped Room' — no rooms. LEFT COMPLETELY UNTOUCHED.
--
-- Take a backup first:
--   sudo mysqldump yanolja_pms > /root/pms-pre-hattonhills-$(date +%Y%m%d-%H%M%S).sql
--
-- Revert with the "REVERT" block at the bottom, or restore that dump.

START TRANSACTION;

-- ---- 1. Rename five surviving room types (ids 1-5) -----------------------
-- Keyed on `code` (stable) rather than name.
UPDATE room_types SET name='Forest Escape Suite',         code='HH-FES',
       description='Hatton Hills, Central Province', base_price=700,
       max_occupancy=2, is_active=1, updated_at=NOW() WHERE code='MV-DDR';

UPDATE room_types SET name='Eco Harmony Suite',           code='HH-ECO',
       description='Hatton Hills, Central Province', base_price=800,
       max_occupancy=2, is_active=1, updated_at=NOW() WHERE code='MV-DTR';

UPDATE room_types SET name='Sunrise Vista Premium Suite', code='HH-SVP',
       description='Hatton Hills, Central Province', base_price=950,
       max_occupancy=2, is_active=1, updated_at=NOW() WHERE code='MV-FAM';

UPDATE room_types SET name='Mount Luxe Chalet',           code='HH-MLX',
       description='Hatton Hills, Central Province', base_price=1150,
       max_occupancy=5, is_active=1, updated_at=NOW() WHERE code='MV-FND';

UPDATE room_types SET name='Mount Monarch Chalet',        code='HH-MON',
       description='Hatton Hills, Central Province', base_price=1400,
       max_occupancy=5, is_active=1, updated_at=NOW() WHERE code='SU-BV';

-- ---- 2. Reassign and renumber all nine rooms ----------------------------
-- MUST run BEFORE step 3, so the four types being retired are empty first.
--
-- Spread across the five surviving types: 2 rooms each for types 1-4, and 1 for
-- type 5. With only one room per type a single booking exhausted that type and
-- the demo showed "no availability"; two rooms keeps it robust. Mount Monarch
-- Chalet keeps exactly one unit on purpose — it is the single flagship, and the
-- knowledge base says so.
--
-- room_number is UNIQUE. The HH-* target values do not collide with any MV-*/SU-*
-- source value, so no intermediate state of this sequence violates the index and
-- no temporary placeholder renames are needed.
UPDATE rooms SET room_number='HH-101', room_type_id=1, updated_at=NOW() WHERE room_number='MV-101';
UPDATE rooms SET room_number='HH-102', room_type_id=1, updated_at=NOW() WHERE room_number='SU-901';
UPDATE rooms SET room_number='HH-201', room_type_id=2, updated_at=NOW() WHERE room_number='MV-102';
UPDATE rooms SET room_number='HH-202', room_type_id=2, updated_at=NOW() WHERE room_number='SU-902';
UPDATE rooms SET room_number='HH-301', room_type_id=3, updated_at=NOW() WHERE room_number='MV-201';
UPDATE rooms SET room_number='HH-302', room_type_id=3, updated_at=NOW() WHERE room_number='SU-903';
UPDATE rooms SET room_number='HH-401', room_type_id=4, updated_at=NOW() WHERE room_number='MV-202';
UPDATE rooms SET room_number='HH-402', room_type_id=4, updated_at=NOW() WHERE room_number='SU-904';
UPDATE rooms SET room_number='HH-501', room_type_id=5, updated_at=NOW() WHERE room_number='SU-905';

-- ---- 3. Retire the four now-empty ex-Mosvold types ----------------------
-- Kept (not deleted) so any historical reservation row still resolves its type.
-- Renamed to neutral placeholders so no Mosvold string survives anywhere, and
-- deactivated. Kavya filters them out regardless: _property_of() returns "" for
-- an unrecognised name, so they can never be quoted or booked.
UPDATE room_types SET name='Retired Room Type A', code='RET-A',
       description='Retired', is_active=0, updated_at=NOW() WHERE code='SU-DDG';
UPDATE room_types SET name='Retired Room Type B', code='RET-B',
       description='Retired', is_active=0, updated_at=NOW() WHERE code='SU-DDS';
UPDATE room_types SET name='Retired Room Type C', code='RET-C',
       description='Retired', is_active=0, updated_at=NOW() WHERE code='SU-DTS';
UPDATE room_types SET name='Retired Room Type D', code='RET-D',
       description='Retired', is_active=0, updated_at=NOW() WHERE code='SU-FVP';

COMMIT;

-- ---- Verify --------------------------------------------------------------
--   SELECT id, name, code, base_price, max_occupancy, is_active
--     FROM room_types ORDER BY id;
-- Expect:
--   1  Forest Escape Suite          HH-FES  700.00   2  1
--   2  Eco Harmony Suite            HH-ECO  800.00   2  1
--   3  Sunrise Vista Premium Suite  HH-SVP  950.00   2  1
--   4  Mount Luxe Chalet            HH-MLX  1150.00  5  1
--   5  Mount Monarch Chalet         HH-MON  1400.00  5  1
--   6  Default Unmapped Room        DUR     0.00     0  (untouched)
--   7  Retired Room Type A          RET-A            0
--   8  Retired Room Type B          RET-B            0
--   9  Retired Room Type C          RET-C            0
--   10 Retired Room Type D          RET-D            0
--
--   SELECT r.room_number, r.room_type_id, t.name
--     FROM rooms r JOIN room_types t ON t.id = r.room_type_id
--     ORDER BY r.room_number;
-- Expect 9 rooms, all HH-*: HH-101/HH-102 -> Forest Escape Suite,
--   HH-201/HH-202 -> Eco Harmony Suite, HH-301/HH-302 -> Sunrise Vista Premium
--   Suite, HH-401/HH-402 -> Mount Luxe Chalet, HH-501 -> Mount Monarch Chalet.
--
--   -- No Mosvold string may survive:
--   SELECT id, name, code FROM room_types
--     WHERE name LIKE '%Deluxe%' OR name LIKE '%Villa%' OR name LIKE '%Founders%'
--        OR code LIKE 'MV-%' OR code LIKE 'SU-%';
-- Expect: empty set.
--
--   SELECT room_number FROM rooms WHERE room_number LIKE 'MV-%' OR room_number LIKE 'SU-%';
-- Expect: empty set.

-- ---- REVERT (restore the nine Mosvold room types) ------------------------
-- START TRANSACTION;
-- -- rooms back to one per type, original numbers
-- UPDATE rooms SET room_number='MV-101', room_type_id=1,  updated_at=NOW() WHERE room_number='HH-101';
-- UPDATE rooms SET room_number='SU-901', room_type_id=7,  updated_at=NOW() WHERE room_number='HH-102';
-- UPDATE rooms SET room_number='MV-102', room_type_id=2,  updated_at=NOW() WHERE room_number='HH-201';
-- UPDATE rooms SET room_number='SU-902', room_type_id=8,  updated_at=NOW() WHERE room_number='HH-202';
-- UPDATE rooms SET room_number='MV-201', room_type_id=3,  updated_at=NOW() WHERE room_number='HH-301';
-- UPDATE rooms SET room_number='SU-903', room_type_id=9,  updated_at=NOW() WHERE room_number='HH-302';
-- UPDATE rooms SET room_number='MV-202', room_type_id=4,  updated_at=NOW() WHERE room_number='HH-401';
-- UPDATE rooms SET room_number='SU-904', room_type_id=10, updated_at=NOW() WHERE room_number='HH-402';
-- UPDATE rooms SET room_number='SU-905', room_type_id=5,  updated_at=NOW() WHERE room_number='HH-501';
-- -- room types back to the Mosvold nine
-- UPDATE room_types SET name='Deluxe Double Room', code='MV-DDR',
--        description='Mosvold Villa, Ahangama', base_price=700, max_occupancy=2,
--        is_active=1, updated_at=NOW() WHERE code='HH-FES';
-- UPDATE room_types SET name='Deluxe Twin Room', code='MV-DTR',
--        description='Mosvold Villa, Ahangama', base_price=700, max_occupancy=2,
--        is_active=1, updated_at=NOW() WHERE code='HH-ECO';
-- UPDATE room_types SET name='Family Suite', code='MV-FAM',
--        description='Mosvold Villa, Ahangama', base_price=820, max_occupancy=4,
--        is_active=1, updated_at=NOW() WHERE code='HH-SVP';
-- UPDATE room_types SET name='Founders Suite', code='MV-FND',
--        description='Mosvold Villa, Ahangama', base_price=900, max_occupancy=2,
--        is_active=1, updated_at=NOW() WHERE code='HH-MLX';
-- UPDATE room_types SET name='Beach Villa', code='SU-BV',
--        description='Sundara by Mosvold, Balapitiya', base_price=880, max_occupancy=4,
--        is_active=1, updated_at=NOW() WHERE code='HH-MON';
-- UPDATE room_types SET name='Deluxe Double Room with Garden View', code='SU-DDG',
--        description='Sundara by Mosvold, Balapitiya', base_price=700, max_occupancy=2,
--        is_active=1, updated_at=NOW() WHERE code='RET-A';
-- UPDATE room_types SET name='Deluxe Double Room with Sea View', code='SU-DDS',
--        description='Sundara by Mosvold, Balapitiya', base_price=760, max_occupancy=2,
--        is_active=1, updated_at=NOW() WHERE code='RET-B';
-- UPDATE room_types SET name='Deluxe Twin Room with Sea View', code='SU-DTS',
--        description='Sundara by Mosvold, Balapitiya', base_price=760, max_occupancy=2,
--        is_active=1, updated_at=NOW() WHERE code='RET-C';
-- UPDATE room_types SET name='Family Villa with Pool', code='SU-FVP',
--        description='Sundara by Mosvold, Balapitiya', base_price=900, max_occupancy=4,
--        is_active=1, updated_at=NOW() WHERE code='RET-D';
-- COMMIT;
-- Then revert Kavya's code too (knowledge_docs/hotel_info.txt, yanolja_service.py,
-- tools.py, server.py) — the PMS alone is not the whole rebrand.
