#!/usr/bin/env bash
# apply_rename.sh — back up, rename PMS room types to Mosvold, verify.
#
# Must run as a user with MySQL access (root). The `dev` user is denied.
#
#   sudo bash /home/dev/full-voice-agent/Kavya/ops/mosvold-pms/apply_rename.sh
#
# Safe to inspect first: it prints the before-state and the backup location,
# and aborts on any error before touching data.

set -euo pipefail

DB="${DB:-yanolja_pms}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SQL="$HERE/rename_to_mosvold.sql"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="/root/pms-pre-mosvold-${STAMP}.sql"

[ -f "$SQL" ] || { echo "ERROR: missing $SQL" >&2; exit 1; }

echo "== Database: $DB"

# ---- Pre-flight: confirm the column casing before touching anything -------
# The models use camelCase attributes but config/database.ts sets a global
# `underscored: true`, so the real columns are snake_case. Verify rather than
# assume — a mismatch here is what makes the migration fail halfway.
echo
echo "== Schema check:"
COLS="$(mysql -N -B "$DB" -e "
  SELECT COLUMN_NAME FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'room_types';")"
printf '   room_types columns: %s\n' "$(echo "$COLS" | tr '\n' ' ')"

MISSING=""
for c in base_price max_occupancy is_active created_at updated_at; do
  printf '%s\n' "$COLS" | grep -qx "$c" || MISSING="$MISSING $c"
done
if [ -n "$MISSING" ]; then
  echo "ERROR: expected snake_case column(s) not found:$MISSING" >&2
  echo "       This DB uses a different casing than the migration assumes." >&2
  echo "       Run 'SHOW COLUMNS FROM room_types;' and adjust the .sql files." >&2
  echo "       Nothing has been modified." >&2
  exit 1
fi
echo "   OK — snake_case columns confirmed, no changes made yet."

echo
echo "== BEFORE:"
mysql "$DB" -e "SELECT id, name, code, base_price, max_occupancy FROM room_types ORDER BY id;"

echo
echo "== Backing up room_types, rooms, reservations -> $BACKUP"
mysqldump "$DB" room_types rooms reservations > "$BACKUP"
chmod 600 "$BACKUP"
echo "   backup size: $(du -h "$BACKUP" | cut -f1)"

echo
echo "== Applying rename..."
mysql "$DB" < "$SQL"

echo
echo "== AFTER:"
mysql "$DB" -e "SELECT id, name, code, base_price, max_occupancy FROM room_types ORDER BY id;"

echo
echo "== Bookable rooms per type (any type showing 0 will report no availability):"
mysql "$DB" -e "
  SELECT rt.name, COUNT(r.id) AS rooms
  FROM room_types rt LEFT JOIN rooms r ON r.room_type_id = rt.id
  GROUP BY rt.id ORDER BY rt.id;"

echo
echo "== Leftover Treehouse names (expect an empty result):"
mysql "$DB" -e "
  SELECT id, name FROM room_types
  WHERE name IN ('Mount Monarch','Mount Luxe','Sunrise Vista',
                 'Eco Harmony','Forest Escape Suite');"

echo
echo "DONE. Rollback if needed:"
echo "  mysql $DB < $HERE/rollback_to_treehouse.sql     # or restore $BACKUP"
