-- set_demo_rates.sql
-- Set indicative DEMO nightly rates on the nine Mosvold room types.
--
-- WHY: mosvoldData.ts seeded every basePrice at 0 because Mosvold publishes no
-- real rate card. book() computes the PMS folio total as base_price * nights,
-- so with 0 the confirmation total is 0. Once Kavya quotes a rate on the call
-- (DEMO_RATES_ENABLED=true), a 0 folio contradicts what the guest just heard.
--
-- THESE FIGURES ARE INVENTED for client demonstrations. They are not a real
-- Mosvold rate card. Keep them in sync with DEMO_NIGHTLY_RATE_USD in
-- yanolja_service.py — if the two disagree, Kavya says one number and the
-- folio shows another.
--
-- Currency: US dollars, per room per night, bed and breakfast, incl. taxes.
--
-- Revert with the "REVERT" block at the bottom (restores 0, the seeded value).

START TRANSACTION;

-- ---- Mosvold Villa (Ahangama) -------------------------------------------
UPDATE room_types SET base_price=700, updated_at=NOW() WHERE code='MV-DDR';  -- Deluxe Double Room
UPDATE room_types SET base_price=700, updated_at=NOW() WHERE code='MV-DTR';  -- Deluxe Twin Room
UPDATE room_types SET base_price=820, updated_at=NOW() WHERE code='MV-FAM';  -- Family Suite
UPDATE room_types SET base_price=900, updated_at=NOW() WHERE code='MV-FND';  -- Founders Suite

-- ---- Sundara by Mosvold (Balapitiya) ------------------------------------
UPDATE room_types SET base_price=700, updated_at=NOW() WHERE code='SU-DDG';  -- Deluxe Double, Garden View
UPDATE room_types SET base_price=760, updated_at=NOW() WHERE code='SU-DDS';  -- Deluxe Double, Sea View
UPDATE room_types SET base_price=760, updated_at=NOW() WHERE code='SU-DTS';  -- Deluxe Twin, Sea View
UPDATE room_types SET base_price=880, updated_at=NOW() WHERE code='SU-BV';   -- Beach Villa
UPDATE room_types SET base_price=900, updated_at=NOW() WHERE code='SU-FVP';  -- Family Villa with Pool

COMMIT;

-- ---- Verify --------------------------------------------------------------
--   SELECT code, name, base_price FROM room_types ORDER BY code;
-- Expect: MV-DDR 700, MV-DTR 700, MV-FAM 820, MV-FND 900,
--         SU-BV 880, SU-DDG 700, SU-DDS 760, SU-DTS 760, SU-FVP 900,
--         DUR 0 (Default Unmapped Room stays 0).

-- ---- REVERT (restore the seeded zero rates) ------------------------------
-- START TRANSACTION;
-- UPDATE room_types SET base_price=0, updated_at=NOW()
--   WHERE code IN ('MV-DDR','MV-DTR','MV-FAM','MV-FND',
--                  'SU-DDG','SU-DDS','SU-DTS','SU-BV','SU-FVP');
-- COMMIT;
-- Also set DEMO_RATES_ENABLED=false in the Kavya .env and recreate the
-- container, or Kavya will keep quoting rates the folio no longer matches.
