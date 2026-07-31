-- seed_room_types.sql — direct seed for the ISOLATED Mosvold PMS MySQL database.
--
-- Use ONLY against a separate database (e.g. `mosvold_pms`) for the Mosvold demo
-- instance. NEVER run against `yanolja_pms` — that database backs the LIVE
-- Treehouse Kavya agent in production.
--
-- Prefer mosvoldData.ts (the app-native seed) when possible; this SQL is the
-- decoupled fallback for seeding a fresh DB without rebuilding the app.
--
-- COLUMN CASING: the models declare camelCase attributes, but
-- backend/src/config/database.ts sets a GLOBAL
--   define: { timestamps: true, underscored: true }
-- so the physical columns are snake_case: `base_price`, `max_occupancy`,
-- `is_active`, `created_at`, `updated_at` (and in `rooms`: `room_number`,
-- `room_type_id`, `housekeeping_status`). Confirm with
--   SHOW COLUMNS FROM room_types;
-- before running, in case the target instance differs.
--
-- Room-type names MUST match Kavya's ROOM_TYPES_BY_PROPERTY byte-for-byte —
-- property identity is derived from the name (this schema has no property column).

INSERT INTO room_types (name, code, description, base_price, max_occupancy, is_active, created_at, updated_at) VALUES
  -- Mosvold Villa (Ahangama)
  ('Deluxe Double Room',                  'MV-DDR', 'Mosvold Villa, Ahangama',   0, 2, 1, NOW(), NOW()),
  ('Deluxe Twin Room',                    'MV-DTR', 'Mosvold Villa, Ahangama',   0, 2, 1, NOW(), NOW()),
  ('Family Suite',                        'MV-FAM', 'Mosvold Villa, Ahangama',   0, 4, 1, NOW(), NOW()),
  ('Founders Suite',                      'MV-FND', 'Mosvold Villa, Ahangama',   0, 2, 1, NOW(), NOW()),
  -- Sundara by Mosvold (Balapitiya)
  ('Deluxe Double Room with Garden View', 'SU-DDG', 'Sundara by Mosvold, Balapitiya', 0, 2, 1, NOW(), NOW()),
  ('Deluxe Double Room with Sea View',    'SU-DDS', 'Sundara by Mosvold, Balapitiya', 0, 2, 1, NOW(), NOW()),
  ('Deluxe Twin Room with Sea View',      'SU-DTS', 'Sundara by Mosvold, Balapitiya', 0, 2, 1, NOW(), NOW()),
  ('Beach Villa',                         'SU-BV',  'Sundara by Mosvold, Balapitiya', 0, 4, 1, NOW(), NOW()),
  ('Family Villa with Pool',              'SU-FVP', 'Sundara by Mosvold, Balapitiya', 0, 4, 1, NOW(), NOW()),
  -- Fallback for unmapped PMS rooms (mirrors demoData)
  ('Default Unmapped Room',               'DUR',    'Unmapped',                  0, 0, 1, NOW(), NOW());

-- One bookable physical room per type (so availability is non-empty). Adjust the
-- room_type_id sub-selects if your codes differ.
INSERT INTO rooms (room_number, room_type_id, floor, status, housekeeping_status, created_at, updated_at) VALUES
  ('MV Deluxe Double 01',  (SELECT id FROM room_types WHERE code='MV-DDR'), 1, 'available', 'clean', NOW(), NOW()),
  ('MV Deluxe Twin 01',    (SELECT id FROM room_types WHERE code='MV-DTR'), 1, 'available', 'clean', NOW(), NOW()),
  ('MV Family Suite 01',   (SELECT id FROM room_types WHERE code='MV-FAM'), 1, 'available', 'clean', NOW(), NOW()),
  ('MV Founders Suite 01', (SELECT id FROM room_types WHERE code='MV-FND'), 2, 'available', 'clean', NOW(), NOW()),
  ('SU Double Garden 01',  (SELECT id FROM room_types WHERE code='SU-DDG'), 1, 'available', 'clean', NOW(), NOW()),
  ('SU Double Sea 01',     (SELECT id FROM room_types WHERE code='SU-DDS'), 1, 'available', 'clean', NOW(), NOW()),
  ('SU Twin Sea 01',       (SELECT id FROM room_types WHERE code='SU-DTS'), 1, 'available', 'clean', NOW(), NOW()),
  ('SU Beach Villa 01',    (SELECT id FROM room_types WHERE code='SU-BV'),  1, 'available', 'clean', NOW(), NOW()),
  ('SU Family Villa 01',   (SELECT id FROM room_types WHERE code='SU-FVP'), 1, 'available', 'clean', NOW(), NOW());
