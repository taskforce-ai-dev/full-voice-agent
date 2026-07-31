-- rollback_to_treehouse.sql
-- Undo rename_to_mosvold.sql: restore the original Treehouse room-type names
-- and remove the four Sundara types that were added.
--
-- Values below are taken from the app's own seed (backend/src/seed/demoData.ts),
-- so they restore the documented Treehouse baseline, including base_price.
--
-- PREFER THE MYSQLDUMP. If you took the backup named in rename_to_mosvold.sql,
-- restoring it is strictly safer than this script, because this script cannot
-- recover any room-type row that did not come from demoData.ts.
--
-- NOTE: this restores NAMES. Reservations created during the Mosvold demo will
-- remain in the table, attached to the renamed types. Review them after
-- rolling back:
--   SELECT id, reservation_no, room_type_id, created_at FROM reservations
--   WHERE created_at >= '<demo start date>';

START TRANSACTION;

-- ---- 1. Drop the rooms added for the four new Sundara types -------------
DELETE FROM rooms WHERE room_number IN ('SU-901', 'SU-902', 'SU-903', 'SU-904');

-- ---- 2. Drop the four added room types ---------------------------------
-- Will fail if a reservation still references one — resolve those first
-- (that failure is deliberate: it prevents orphaning booking rows).
DELETE FROM room_types WHERE code IN ('SU-DDG', 'SU-DDS', 'SU-DTS', 'SU-FVP');

-- ---- 3. Restore the five renamed types ---------------------------------
UPDATE room_types SET name='Mount Monarch', code='MON', base_price=225,
       max_occupancy=2, is_active=1, updated_at=NOW() WHERE code='MV-DDR';
UPDATE room_types SET name='Mount Luxe', code='LUX', base_price=225,
       max_occupancy=2, is_active=1, updated_at=NOW() WHERE code='MV-DTR';
UPDATE room_types SET name='Sunrise Vista', code='SV', base_price=139,
       max_occupancy=3, is_active=1, updated_at=NOW() WHERE code='MV-FAM';
UPDATE room_types SET name='Eco Harmony', code='ECO', base_price=119,
       max_occupancy=2, is_active=1, updated_at=NOW() WHERE code='MV-FND';
UPDATE room_types SET name='Forest Escape Suite', code='FAM', base_price=0,
       max_occupancy=5, is_active=1, updated_at=NOW() WHERE code='SU-BV';

COMMIT;

-- ---- 4. Verify ---------------------------------------------------------
--   SELECT id, name, code, base_price, max_occupancy FROM room_types ORDER BY id;
-- Expect: Mount Monarch, Mount Luxe, Sunrise Vista, Eco Harmony,
--         Forest Escape Suite, Default Unmapped Room — and no MV-/SU- codes.
