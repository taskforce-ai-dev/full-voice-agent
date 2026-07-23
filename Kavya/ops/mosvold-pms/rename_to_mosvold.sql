-- rename_to_mosvold.sql
-- Rename the existing PMS room types to the nine Mosvold names, in place.
--
-- WHY THIS EXISTS: the PMS exposes NO room-type API endpoint (room.routes.ts
-- only has /rooms CRUD; GET/PUT /api/room-types -> 404). Room-type names can
-- therefore only be changed in the database. Requires root/MySQL access.
--
-- ============================ READ THIS FIRST ============================
-- TARGET: `yanolja_pms` (the existing shared PMS database).
-- Authorised 2026-07-23: the Treehouse agent is retired and no longer serving
-- callers, so renaming its room types in place is acceptable.
--
-- This still rewrites room_types IN PLACE. `reservations.roomTypeId` and
-- `rooms.roomTypeId` are foreign keys, so ANY EXISTING TREEHOUSE RESERVATIONS
-- WILL RETROACTIVELY DISPLAY MOSVOLD NAMES. The booking rows survive; their
-- room-type label does not. That is accepted here because Treehouse is retired.
--
-- If Treehouse is ever brought back, restore from the mysqldump below or run
-- rollback_to_treehouse.sql.
--
-- TAKE A BACKUP FIRST:
--   mysqldump yanolja_pms room_types rooms reservations > ~/pms-pre-mosvold.sql
--
-- Names MUST match Kavya's ROOM_TYPES_BY_PROPERTY byte-for-byte — property
-- identity is derived from the room-type NAME (this schema has no property
-- column). Do not "tidy" these strings.
--
-- COLUMN CASING: models use camelCase with tableName 'room_types' and NO
-- `underscored: true`, so columns are basePrice / maxOccupancy / isActive /
-- createdAt / updatedAt. Verify before running:
--   SHOW COLUMNS FROM room_types;
-- =========================================================================

-- ---- 0. Pre-flight: see exactly what you are about to overwrite ---------
-- Run this on its own FIRST and confirm the five codes below are present.
--   SELECT id, name, code, basePrice, maxOccupancy, isActive FROM room_types ORDER BY id;

START TRANSACTION;

-- ---- 1. Rename the 5 existing Treehouse types -> Mosvold ----------------
-- Keyed on `code` (stable) rather than name. basePrice is zeroed because the
-- Mosvold demo quotes no rates; Kavya never reads basePrice.

-- Mosvold Villa (Ahangama)
UPDATE room_types SET name='Deluxe Double Room', code='MV-DDR',
       description='Mosvold Villa, Ahangama', basePrice=0, maxOccupancy=2,
       isActive=1, updatedAt=NOW() WHERE code='MON';
UPDATE room_types SET name='Deluxe Twin Room', code='MV-DTR',
       description='Mosvold Villa, Ahangama', basePrice=0, maxOccupancy=2,
       isActive=1, updatedAt=NOW() WHERE code='LUX';
UPDATE room_types SET name='Family Suite', code='MV-FAM',
       description='Mosvold Villa, Ahangama', basePrice=0, maxOccupancy=4,
       isActive=1, updatedAt=NOW() WHERE code='SV';
UPDATE room_types SET name='Founders Suite', code='MV-FND',
       description='Mosvold Villa, Ahangama', basePrice=0, maxOccupancy=2,
       isActive=1, updatedAt=NOW() WHERE code='ECO';

-- Sundara by Mosvold (Balapitiya)
UPDATE room_types SET name='Beach Villa', code='SU-BV',
       description='Sundara by Mosvold, Balapitiya', basePrice=0, maxOccupancy=4,
       isActive=1, updatedAt=NOW() WHERE code='FAM';

-- ---- 2. Add the 4 remaining Sundara types ------------------------------
INSERT INTO room_types (name, code, description, basePrice, maxOccupancy, isActive, createdAt, updatedAt) VALUES
  ('Deluxe Double Room with Garden View', 'SU-DDG', 'Sundara by Mosvold, Balapitiya', 0, 2, 1, NOW(), NOW()),
  ('Deluxe Double Room with Sea View',    'SU-DDS', 'Sundara by Mosvold, Balapitiya', 0, 2, 1, NOW(), NOW()),
  ('Deluxe Twin Room with Sea View',      'SU-DTS', 'Sundara by Mosvold, Balapitiya', 0, 2, 1, NOW(), NOW()),
  ('Family Villa with Pool',              'SU-FVP', 'Sundara by Mosvold, Balapitiya', 0, 4, 1, NOW(), NOW());

-- ---- 3. Give the 4 new types a bookable physical room ------------------
-- The 5 renamed types keep their existing rooms automatically (roomTypeId is
-- unchanged by a rename). roomNumber is UNIQUE — these use a 9xx block to
-- avoid colliding with existing room numbers.
INSERT INTO rooms (roomNumber, roomTypeId, floor, status, housekeepingStatus, createdAt, updatedAt) VALUES
  ('SU-901', (SELECT id FROM room_types WHERE code='SU-DDG'), 1, 'available', 'clean', NOW(), NOW()),
  ('SU-902', (SELECT id FROM room_types WHERE code='SU-DDS'), 1, 'available', 'clean', NOW(), NOW()),
  ('SU-903', (SELECT id FROM room_types WHERE code='SU-DTS'), 1, 'available', 'clean', NOW(), NOW()),
  ('SU-904', (SELECT id FROM room_types WHERE code='SU-FVP'), 1, 'available', 'clean', NOW(), NOW());

COMMIT;

-- ---- 4. Verify ---------------------------------------------------------
-- Expect exactly the nine Mosvold names + 'Default Unmapped Room',
-- and NO Treehouse names (Mount Monarch / Mount Luxe / Sunrise Vista /
-- Eco Harmony / Forest Escape Suite):
--
--   SELECT id, name, code, maxOccupancy FROM room_types ORDER BY id;
--
-- Every type should have >= 1 available room, or Kavya reports zero
-- availability for it:
--
--   SELECT rt.name, COUNT(r.id) AS rooms
--   FROM room_types rt LEFT JOIN rooms r ON r.roomTypeId = rt.id
--   GROUP BY rt.id ORDER BY rt.id;
