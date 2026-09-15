-- seed_riviera_room_types.sql
-- Seed the Riviera Resort room inventory into a FRESH, DEDICATED Yanolja PMS
-- instance (the TaskForce `booking-pms` app + its `yanolja_pms` MySQL schema).
--
-- *** NEVER RUN THIS AGAINST KAVYA'S DATABASE. ***
-- The PMS schema has no property column: room-type NAMES are the single source
-- of property identity, so Kavya (Hatton Hills) and Riviera can only share a
-- PMS process if they share a room catalogue, which they must not. Riviera
-- gets its own PMS instance (own MySQL schema, own PM2 process/port, own
-- hostname behind nginx) and this file seeds THAT instance. See RUNBOOK.md.
--
-- ROOM NAMES ARE LOAD-BEARING. These seven strings must match Riviera's
-- yanolja_service.ROOM_TYPES_BY_PROPERTY and tools.ROOM_TYPES_BY_PROPERTY
-- BYTE-FOR-BYTE. yanolja_service._property_of() returns "" for any name it does
-- not recognise, and rows returning "" are filtered out of availability
-- entirely. A single typo silently removes a room type from Riya's inventory.
--
-- COLUMN CASING: the Sequelize models declare camelCase attributes (basePrice,
-- maxOccupancy, isActive, roomNumber, roomTypeId) but the physical MySQL
-- columns are snake_case. CONFIRM BEFORE RUNNING:
--   SHOW COLUMNS FROM room_types;
--   SHOW COLUMNS FROM rooms;
-- and adjust the identifiers if this instance differs.
--
-- PRICES: base_price is the MID-SEASON Sri Lankan resident room-only rate in
-- LKR (yanolja_service.NIGHTLY_RATE_LKR[room]["mid"]). The PMS has one price
-- column and the agent quotes from its own two-season catalogue, so the folio
-- shows the mid-season figure and Riya quotes the season-correct one; the high
-- season is 1 Jul–31 Aug 2027 only. Keep base_price in sync with the "mid"
-- entries in yanolja_service.py, and RATES_ENABLED=true in Riviera's env.
--
-- Room numbers come from the resort's rate sheet ("Rooms / Nos"). The sheet
-- lists "Rm 9" under BOTH Triple Garden View and Family Cottage; the cottage is
-- seeded as RV-8 as a placeholder — CONFIRM WITH THE RESORT and fix it here
-- and in yanolja_service.ROOM_NUMBERS.
--
-- Take a backup first if the instance is not empty:
--   sudo mysqldump yanolja_pms > /root/pms-pre-riviera-$(date +%Y%m%d-%H%M%S).sql

START TRANSACTION;

-- ---- 1. Room types ---------------------------------------------------------
INSERT INTO room_types (name, code, description, base_price, max_occupancy, is_active, created_at, updated_at) VALUES
  ('Family Chalet',                'RV-FCH', 'Riviera Resort, Kallady, Batticaloa — modern A-frame family chalet, lagoon views, A/C', 32100, 4, 1, NOW(), NOW()),
  ('Basic Room Single',            'RV-BRS', 'Riviera Resort, Kallady, Batticaloa — basic air-conditioned room',                  8800, 2, 1, NOW(), NOW()),
  ('Wooden Cabana',                'RV-WCB', 'Riviera Resort, Kallady, Batticaloa — traditional wooden cabana on stilts, fan only, NO A/C', 11000, 2, 1, NOW(), NOW()),
  ('Lagoon View Steel Cabana',     'RV-SCB', 'Riviera Resort, Kallady, Batticaloa — container-conversion steel cabana, lagoon view, A/C', 14600, 2, 1, NOW(), NOW()),
  ('Double Lagoon or Garden View', 'RV-DBL', 'Riviera Resort, Kallady, Batticaloa — classic double, lagoon or garden view, A/C, extra bed on request', 17500, 3, 1, NOW(), NOW()),
  ('Triple Garden View',           'RV-TRP', 'Riviera Resort, Kallady, Batticaloa — first-floor triple, garden view, A/C',      20400, 3, 1, NOW(), NOW()),
  ('Family Cottage',               'RV-FCT', 'Riviera Resort, Kallady, Batticaloa — standalone lagoon-front cottage, sleeps five, A/C', 24800, 5, 1, NOW(), NOW());

-- ---- 2. Rooms ----------------------------------------------------------------
-- room_number is UNIQUE. Resolve room_type_id by code so this file does not
-- depend on auto-increment values.
INSERT INTO rooms (room_number, room_type_id, housekeeping_status, created_at, updated_at)
SELECT r.room_number, t.id, 'clean', NOW(), NOW()
FROM (
  SELECT 'RV-36' AS room_number, 'RV-FCH' AS code UNION ALL
  SELECT 'RV-37', 'RV-FCH' UNION ALL
  SELECT 'RV-7',  'RV-BRS' UNION ALL
  SELECT 'RV-10', 'RV-BRS' UNION ALL
  SELECT 'RV-11', 'RV-BRS' UNION ALL
  SELECT 'RV-12', 'RV-WCB' UNION ALL
  SELECT 'RV-31', 'RV-SCB' UNION ALL
  SELECT 'RV-32', 'RV-SCB' UNION ALL
  SELECT 'RV-33', 'RV-SCB' UNION ALL
  SELECT 'RV-34', 'RV-SCB' UNION ALL
  SELECT 'RV-4',  'RV-DBL' UNION ALL
  SELECT 'RV-5',  'RV-DBL' UNION ALL
  SELECT 'RV-6',  'RV-DBL' UNION ALL
  SELECT 'RV-15', 'RV-DBL' UNION ALL
  SELECT 'RV-16', 'RV-DBL' UNION ALL
  SELECT 'RV-18', 'RV-DBL' UNION ALL
  SELECT 'RV-19', 'RV-DBL' UNION ALL
  SELECT 'RV-20', 'RV-TRP' UNION ALL
  SELECT 'RV-9',  'RV-TRP' UNION ALL
  SELECT 'RV-8',  'RV-FCT'      -- placeholder: sheet lists "Rm 9" twice, confirm with the resort
) AS r
JOIN room_types t ON t.code = r.code;

COMMIT;

-- ---- Verify ------------------------------------------------------------------
--   SELECT id, name, code, base_price, max_occupancy, is_active
--     FROM room_types ORDER BY id;
-- Expect exactly the seven names above, all is_active=1.
--
--   SELECT r.room_number, t.name FROM rooms r JOIN room_types t ON t.id = r.room_type_id
--     ORDER BY t.id, r.room_number;
-- Expect 20 rooms, all RV-*.
--
-- Then run the agent-side verifier (it exercises Riviera's own name matching):
--   cd Riviera && python ops/riviera-pms/verify_live.py

-- ---- Revert ------------------------------------------------------------------
-- Only safe on a dedicated Riviera instance with no reservations yet:
--   DELETE FROM rooms WHERE room_number LIKE 'RV-%';
--   DELETE FROM room_types WHERE code LIKE 'RV-%';
