-- rollback_to_treehouse.sql
-- Undo rename_to_mosvold.sql: restore the original Treehouse room-type names
-- and remove the four Sundara types that were added.
--
-- Values below are taken from the app's own seed (backend/src/seed/demoData.ts),
-- so they restore the documented Treehouse baseline, including basePrice.
--
-- PREFER THE MYSQLDUMP. If you took the backup named in rename_to_mosvold.sql,
-- restoring it is strictly safer than this script, because this script cannot
-- recover any room-type row that did not come from demoData.ts.
--
-- NOTE: this restores NAMES. Reservations created during the Mosvold demo will
-- remain in the table, attached to the renamed types. Review them after
-- rolling back:
--   SELECT id, reservationNo, roomTypeId, createdAt FROM reservations
--   WHERE createdAt >= '<demo start date>';

START TRANSACTION;

-- ---- 1. Drop the rooms added for the four new Sundara types -------------
DELETE FROM rooms WHERE roomNumber IN ('SU-901', 'SU-902', 'SU-903', 'SU-904');

-- ---- 2. Drop the four added room types ---------------------------------
-- Will fail if a reservation still references one — resolve those first
-- (that failure is deliberate: it prevents orphaning booking rows).
DELETE FROM room_types WHERE code IN ('SU-DDG', 'SU-DDS', 'SU-DTS', 'SU-FVP');

-- ---- 3. Restore the five renamed types ---------------------------------
UPDATE room_types SET name='Mount Monarch', code='MON', basePrice=225,
       maxOccupancy=2, isActive=1, updatedAt=NOW() WHERE code='MV-DDR';
UPDATE room_types SET name='Mount Luxe', code='LUX', basePrice=225,
       maxOccupancy=2, isActive=1, updatedAt=NOW() WHERE code='MV-DTR';
UPDATE room_types SET name='Sunrise Vista', code='SV', basePrice=139,
       maxOccupancy=3, isActive=1, updatedAt=NOW() WHERE code='MV-FAM';
UPDATE room_types SET name='Eco Harmony', code='ECO', basePrice=119,
       maxOccupancy=2, isActive=1, updatedAt=NOW() WHERE code='MV-FND';
UPDATE room_types SET name='Forest Escape Suite', code='FAM', basePrice=0,
       maxOccupancy=5, isActive=1, updatedAt=NOW() WHERE code='SU-BV';

COMMIT;

-- ---- 4. Verify ---------------------------------------------------------
--   SELECT id, name, code, basePrice, maxOccupancy FROM room_types ORDER BY id;
-- Expect: Mount Monarch, Mount Luxe, Sunrise Vista, Eco Harmony,
--         Forest Escape Suite, Default Unmapped Room — and no MV-/SU- codes.
