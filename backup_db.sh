#!/usr/bin/env bash
# Back up your screenshot-digest SQLite database to a PRIVATE git repo.
#
# Why a script (not just `git add`): the DB stores OCR'd text in cleartext, so
# the backup MUST go somewhere private. And copying a live SQLite file mid-write
# can capture a torn, unopenable snapshot — so this uses the sqlite3 `.backup`
# command, which makes a safe, consistent copy even while the DB is being written.
#
# Usage:
#   export BACKUP_REPO=git@github.com:you/screenshots-db-backup.git   # PRIVATE repo
#   ./backup_db.sh
#
# Schedule it nightly with cron (see README → "Back up your database").
#
# Env:
#   BACKUP_REPO   (required)  git remote of a PRIVATE backup repo
#   DIGEST_HOME   (optional)  where the DB lives; default $SCREENSHOT_DIGEST_HOME
#                             or ~/.screenshot-digest
#   BACKUP_DIR    (optional)  local working clone; default ~/.cache/screenshot-backup

set -euo pipefail

: "${BACKUP_REPO:?set BACKUP_REPO to a PRIVATE git remote, e.g. git@github.com:you/screenshots-db-backup.git}"
DIGEST_HOME="${DIGEST_HOME:-${SCREENSHOT_DIGEST_HOME:-$HOME/.screenshot-digest}}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/.cache/screenshot-backup}"
STAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Clone the backup repo once, then reuse it.
if [ -d "$BACKUP_DIR/.git" ]; then
    git -C "$BACKUP_DIR" pull --quiet || true   # ok if the remote has no commits yet
else
    git clone "$BACKUP_REPO" "$BACKUP_DIR"
fi

# Snapshot every valid SQLite .db in DIGEST_HOME (skips non-SQLite files).
for db in "$DIGEST_HOME"/*.db; do
    [ -f "$db" ] || continue
    name=$(basename "$db")
    if ! sqlite3 "$db" "SELECT 1;" > /dev/null 2>&1; then
        echo "[backup] skipping $name (not a valid SQLite database)"
        continue
    fi
    snap="$(mktemp -t "snap_${name}.XXXXXX")"
    sqlite3 "$db" ".backup $snap"        # consistent snapshot, safe during writes
    gzip -c "$snap" > "$BACKUP_DIR/${name}.gz"
    rm -f "$snap"
    echo "[backup] $name -> ${name}.gz ($(du -h "$BACKUP_DIR/${name}.gz" | cut -f1))"
done

cd "$BACKUP_DIR"
git add -A
if git diff --cached --quiet; then
    echo "[backup] no changes"
else
    git commit -q -m "backup: $STAMP"
    git push -q
    echo "[backup] pushed at $STAMP"
fi
