-- rename_room_labels.sql
-- Follow-up to rename_to_mosvold.sql.
--
-- That migration renamed the room TYPES, but the five pre-existing physical
-- rooms kept their Treehouse-era `rooms.room_number` labels:
--
--   Mount Monarch Chalet-C01   -> type "Deluxe Double Room"   (Mosvold Villa)
--   Mount Luxe Chalet-C02      -> type "Deluxe Twin Room"     (Mosvold Villa)
--   Sunrise Vista Suite-S01    -> type "Family Suite"         (Mosvold Villa)
--   Eco Harmony Suite-S03      -> type "Founders Suite"       (Mosvold Villa)
--   Family Suite-S02           -> type "Beach Villa"          (Sundara)
--
-- Kavya does not read room_number aloud, so this is cosmetic for the phone
-- demo — but the labels ARE visible in the PMS dashboard, and "Mount Monarch
-- Chalet-C01" on screen during a Mosvold demo is a Treehouse leak. The last
-- one is actively confusing: a room labelled "Family Suite-S02" whose type is
-- "Beach Villa".
--
-- New scheme matches the SU-9xx rooms added by the earlier migration:
--   MV-1xx / MV-2xx = Mosvold Villa,  SU-9xx = Sundara by Mosvold.
--
-- `rooms.room_number` is UNIQUE; none of the new values collide with the
-- existing SU-901..SU-904.
--
-- Columns are snake_case (config/database.ts sets `underscored: true`).
-- Safe to re-run: the WHERE clauses match the old labels only.

START TRANSACTION;

-- Mosvold Villa (Ahangama)
UPDATE rooms SET room_number='MV-101', updated_at=NOW()
  WHERE room_number='Mount Monarch Chalet-C01';
UPDATE rooms SET room_number='MV-102', updated_at=NOW()
  WHERE room_number='Mount Luxe Chalet-C02';
UPDATE rooms SET room_number='MV-201', updated_at=NOW()
  WHERE room_number='Sunrise Vista Suite-S01';
UPDATE rooms SET room_number='MV-202', updated_at=NOW()
  WHERE room_number='Eco Harmony Suite-S03';

-- Sundara by Mosvold (Balapitiya)
UPDATE rooms SET room_number='SU-905', updated_at=NOW()
  WHERE room_number='Family Suite-S02';

COMMIT;

-- Verify: every label should be MV-/SU-, and each paired with a Mosvold type.
--   SELECT r.room_number, rt.name AS room_type
--   FROM rooms r JOIN room_types rt ON rt.id = r.room_type_id
--   ORDER BY r.room_number;
--
-- Expect no remaining Treehouse labels:
--   SELECT room_number FROM rooms
--   WHERE room_number REGEXP 'Mount|Sunrise|Eco Harmony|Forest|Chalet|Suite-S';
