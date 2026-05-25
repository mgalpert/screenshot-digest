# 📸 screenshot-digest

Turn your screenshot graveyard into a searchable, auto-categorized, reviewable library.

`screenshot-digest` finds your screenshots (macOS **Photos** library + **Desktop**),
reads the text inside them (OCR), classifies and summarizes each one, and stores it
all in a local SQLite database. From there you get two ways to work through them:

- a **daily markdown digest** grouped by action and category, and
- a **visual web viewer** — an inbox-style grid to filter, triage, and act on shots
  (keyboard-driven, light/dark, multi-select), with optional "send to an AI assistant."

Everything runs locally except the optional Gemini OCR call. **No images leave your
machine unless you enable the Gemini engine** (which sends the image to Google's API
for OCR); the fully-local engine keeps everything on-device.

---

## What actually happens

```
DISCOVERY ─────────► OCR + CLASSIFY ─────────► CACHE ─────────► REVIEW
Photos library        Gemini Flash             SQLite           daily digest (md)
+ ~/Desktop           (or on-device OCR)        dedup by uuid    + visual viewer
```

1. **Discover** — enumerate screenshots from the Photos library and/or `~/Desktop`.
2. **OCR + classify** — extract the text, write a one-line summary, assign a category
   (receipt, code, contact, calendar event, article, social, map, …) and a keep/
   review/delete flag.
3. **Cache** — everything is stored in SQLite, deduped by image uuid, so re-runs are
   cheap and incremental (only new screenshots get processed).
4. **Review** — read the daily digest, or open the viewer to triage visually.

---

## What you need

| Requirement | Notes |
|-------------|-------|
| **macOS** | Reads the Photos library via `osxphotos`; uses Apple Vision for local OCR. |
| **Python 3.10+** | Standard CPython. |
| **Photos / Full Disk access** | Grant your terminal access in System Settings → Privacy & Security, so it can read the Photos library. |
| **Gemini API key** *(optional)* | For the best OCR. Without it, the tool falls back to fully-local OCR automatically. |
| **Pillow** *(optional)* | Faster viewer thumbnails — downscales images to ~900px JPEGs for the grid. Without it the viewer serves the raw full-size files. Viewer-only; not used for OCR. |

**OCR engines:**

| Engine | OCR quality | Cost | Privacy |
|--------|-------------|------|---------|
| **Gemini Flash** (default) | Best — handles infographics, dark UI, dense tables | ~$0.0004/screenshot | Image sent to Google API |
| **Local** (`--local`) | Apple Vision (`ocrmac`); good on plain text, weaker on complex images | Free | 100% on-device |

> **iCloud note:** if your Photos use "Optimize Mac Storage," originals live in
> iCloud. The tool automatically uses the locally-cached preview (plenty for OCR) —
> no downloads required.

---

## Getting started

```bash
# 1. Clone + install
git clone https://github.com/mgalpert/screenshot-digest.git
cd screenshot-digest
pip install -r requirements.txt        # osxphotos + ocrmac (Apple Vision OCR)
pip install pillow                     # optional: faster viewer thumbnails

# 2. Grant Photos / Full Disk access to your terminal (System Settings → Privacy)

# 3. (Optional) add a Gemini key for best OCR — otherwise it runs fully local
export GEMINI_OCR_KEY=your_key_here    # or: echo 'GEMINI_OCR_KEY=...' > .env

# 4. Import your BACKLOG once (the whole library + Desktop), then a report
python3 screenshot_digest.py --all --desktop --report

# 5. Browse + triage in the viewer
python3 screenshot_viewer.py           # → http://127.0.0.1:8765
```

After the one-time `--all` import, keep up with **new** screenshots by running the
plain command (last-24h lookback) on a schedule — see [Automate](#automate). The
viewer reads the same DB live, so new shots show up as soon as they're processed.

> **Backlog vs. forward:** `--all` ingests your *entire* history; the default (no
> `--all`) only looks back 1 day. So yes — you can process everything you already
> have, not just screenshots taken from now on.

---

## Bulk backfill for large libraries (`screenshot_backfill.py`)

`screenshot_digest.py --all` processes serially — fine for a few hundred shots, slow
for tens of thousands. For a big backlog use the dedicated backfill runner, which is
built to grind through a huge library safely while you keep using your machine:

```bash
python3 screenshot_backfill.py                 # whole library + Desktop
python3 screenshot_backfill.py --limit 20      # smoke-test on 20 unprocessed shots
python3 screenshot_backfill.py --workers 6 --rpm 600
python3 screenshot_backfill.py --status        # how many are done, then exit
```

What it does for you:

- **Resumable** — skips anything already in the DB and commits after every row, so
  you can kill it (Ctrl-C, reboot, crash) and just re-run to pick up where it stopped.
- **Won't get rate-limited** — an adaptive throttle widens request spacing on a 429/503
  and narrows it again after a clean streak, converging on the fastest sustainable rate.
- **Won't hang the run** — short per-request timeout + capped retries; a stuck network
  read falls back to local OCR instead of pinning a worker.
- **Gentle on the machine** — a small bounded worker pool (default 6); run it under
  `nice` and it stays out of your way. The Gemini path does OCR server-side, so local
  CPU stays near-idle.
- **Visible** — logs progress + ETA to `backfill.log` (next to the DB), and the viewer
  shows a **live progress bar** while it runs (see below).

---

## CLI reference (`screenshot_digest.py`)

```bash
python3 screenshot_digest.py --report                 # last 24h (Photos), write md report
python3 screenshot_digest.py --desktop --report       # also include ~/Desktop screenshots
python3 screenshot_digest.py --days 7 --desktop --report
python3 screenshot_digest.py --all --desktop --report # reprocess everything (the backlog)
python3 screenshot_digest.py --local --report         # fully offline, nothing leaves the machine
python3 screenshot_digest.py --show "receipt"         # dump stored OCR text matching a substring
```

| Flag | Effect |
|------|--------|
| `--days N` | Lookback window in days (default 1) |
| `--all` | Process the whole library/backlog, ignore the cache |
| `--desktop` | Also scan `~/Desktop/Screenshot*.png` |
| `--report` | Write a markdown report instead of printing |
| `--local` | Force offline (Apple Vision OCR + rules), skip Gemini |
| `--show SUBSTR` | Dump stored OCR text for filenames matching SUBSTR |

**Environment:**

| Var | Default | Purpose |
|-----|---------|---------|
| `SCREENSHOT_DIGEST_HOME` | `~/.screenshot-digest` | Where the DB + reports live |
| `GEMINI_OCR_KEY` | — | Gemini API key (falls back to local if unset) |
| `SCREENSHOT_GEMINI_MODEL` | `gemini-3.5-flash` | Which Gemini model to use |

---

## Visual viewer (`screenshot_viewer.py`)

A self-contained localhost web app (Python **stdlib only**) that reads the same DB.
It's built for working *through* a backlog like an inbox.

```bash
python3 screenshot_viewer.py            # → http://127.0.0.1:8765
python3 screenshot_viewer.py --port 9000
```

- **Visual grid** with thumbnails, a **size toggle** (S/M/L/XL), and **light/dark
  mode** (follows your OS, with a manual toggle that's remembered).
- **Live text filter** across OCR text, summary, filename, and category.
- **Filter chips** for triage status, **category**, and **source** (📱 iPhone /
  🖥 Desktop). Each card shows its source badge and capture date.
- **Date filter** — `7d` / `30d` / `1y` quick presets plus a custom From/To range,
  filtering by capture date. Composes with every other filter.
- **Sort toggle** — newest-first ↔ oldest-first by capture date (remembered).
- **Live backfill progress bar** — while `screenshot_backfill.py` is running, a bar
  shows done / total / rate / ETA and the grid fills in as shots are processed. Hidden
  when no backfill is running.
- **Triage lifecycle.** Every shot is **Needs review → Reviewed → Archived**. New
  shots land in *Needs review* and the view opens to that queue (an inbox). Acting on
  a shot — sending it to the bot, or setting a status — moves it along; *Archived* is
  the gentle "done" state (dimmed, not deleted).
- **Multi-select + bulk actions.** Pick many shots (checkbox, **Select all**, or the
  keyboard), then archive / mark reviewed / send them to the bot in one go.
- **Click any shot** for the full-resolution image, a **download original** link, an
  **editable OCR text** box, and inline recategorize / restatus — all saved straight
  to the DB. Editing OCR then sending overwrites the stored text, so you clean up data
  as you triage.

> Thumbnails are snappier with Pillow (`pip install pillow`); without it the viewer
> serves the raw images.

### Keyboard shortcuts (mouse-free triage)

Press **`?`** in the viewer for the in-app cheatsheet. Actions apply to your
selection if you have one, otherwise the focused card.

| Context | Keys | Action |
|---------|------|--------|
| Grid | `j` `k` / arrows | Move focus cursor |
| Grid | `x` / `space` | Select / deselect (auto-advances) |
| Grid | `⌘A` / `ctrl A` | Select all shown |
| Grid | `a` · `r` · `u` | Archive · Reviewed · back to Needs-review |
| Grid | `s` | Jump to the send box (`Enter` sends to bot) |
| Grid | `o` / `Enter` | Open focused · `g`/`G` first/last · `/` search |
| Open shot | `j` `k` / arrows | Next / previous shot |
| Open shot | `a` `r` `u` · `⌘Enter` | Set status · send to bot |
| Anywhere | `Esc` | Clear selection / close |

### "Send to bot" actions (optional, off by default)

Hand a screenshot to an AI assistant with a free-text instruction — *"add this event
to my calendar,"* *"find ticket prices,"* *"draft a reply."* Checkboxes pick exactly
what to send (metadata / OCR text / summary / the image). The image is the only part
that costs vision tokens, so it's **off by default** — the text is usually enough.

Wire it to any assistant two ways:

```bash
# A) Any CLI/script — the prompt is piped on stdin, or replaces {prompt}
export SCREENSHOT_BOT_NAME="my assistant"
export SCREENSHOT_ACTION_CMD='my-cli chat --stdin'

# B) OpenClaw (https://openclaw.ai) — runs a real agent turn with tools
#    (calendar, web, email) and delivers the reply to a chat channel
export SCREENSHOT_BOT_NAME="Pal"
export SCREENSHOT_ACTION_TARGET="<your chat id>"
export SCREENSHOT_ACTION_CHANNEL="telegram"   # default
export SCREENSHOT_ACTION_AGENT="main"         # default
```

The action panel stays hidden until one of these is configured.

**Quick messages.** Instead of hardcoded prompts, the panel shows *your* reusable
instructions. Type one and hit **+ save current as quick message**; it's stored in
`quick_messages.json` next to the DB and shown as a one-click chip on every shot (the
`×` removes it). Starts empty — it fills with whatever you actually use.

---

## Automate

Import the backlog once, then keep up nightly with `cron`:

```cron
# every night at 20:05 — process new screenshots from the last day
5 20 * * *  /usr/bin/python3 /path/to/screenshot_digest.py --desktop --report
```

---

## For AI agents / automation

A compact, machine-readable summary so an agent can drive this without reading prose.

```yaml
platform: macOS, Python 3.10+
entrypoints:
  ingest: python3 screenshot_digest.py [--all|--days N] [--desktop] [--local] [--report]
  viewer: python3 screenshot_viewer.py [--port 8765] [--host 127.0.0.1]
data_home: $SCREENSHOT_DIGEST_HOME (default ~/.screenshot-digest)
database: $data_home/screenshots.db   # SQLite, table: screenshots
quick_messages: $data_home/quick_messages.json   # JSON array of reusable instructions
action_log: $data_home/viewer_actions.log

db_columns: [uuid, path, filename, date_taken, date_added, date_processed,
             ocr_text, category, summary, source, flag, status]
             # (a legacy `reviewed` int column may also exist on older DBs)
status_values: [needs_review, reviewed, archived]   # viewer triage lifecycle
source_values: [photos, desktop]

viewer_http_api:                      # localhost only
  GET  /api/screenshots               # -> rows WITHOUT full ocr_text (ships ocr_len)
  GET  /api/ocr?id=<uuid>             # -> {ocr_text} for one shot (lazy-loaded)
  GET  /api/search?q=<term>           # -> {uuids:[...]} full-text match, server-side
  GET  /api/quickmsgs                 # -> {messages: [...]}
  GET  /img?id=<uuid>[&full=1][&download=1]   # thumbnails disk-cached by path+mtime+size
  POST /api/update   {uuid, category?|status?|summary?|ocr_text?}
  POST /api/bulk     {uuids:[...], status}            # batch triage
  POST /api/action   {uuid, instruction, include{meta,ocr,summary,image}, ocr?}
  POST /api/quickmsgs {text} | {op:"delete", text}

action_env:                           # enables "send to bot" (off until set)
  generic: SCREENSHOT_ACTION_CMD='cli --stdin'   # prompt on stdin, or {prompt} token
  openclaw: SCREENSHOT_ACTION_TARGET, SCREENSHOT_ACTION_CHANNEL, SCREENSHOT_ACTION_AGENT
  label: SCREENSHOT_BOT_NAME
ocr_env: GEMINI_OCR_KEY (optional), SCREENSHOT_GEMINI_MODEL
notes:
  - re-runs are incremental (deduped by uuid); use --all to reprocess everything
  - reprocessing PRESERVES each shot's triage `status` (only OCR/category/summary refresh)
  - `flag` = AI's advisory keep/review/delete; `status` = authoritative triage state
  - uuids may contain spaces/colons/U+202F — always pass via ?id= (URL-encoded)
  - sending a shot to the bot, or POST /api/bulk, persists status server-side
  - grid thumbnails are cached under $data_home/thumb-cache (safe to delete)
```

---

## The data is yours

Results live in a plain SQLite DB you can query:

```bash
sqlite3 ~/.screenshot-digest/screenshots.db \
  "SELECT filename, summary FROM screenshots WHERE category='receipt_financial';"
```

> ⚠️ **Privacy heads-up:** the DB stores OCR'd text in cleartext — meaning any
> passwords, 2FA codes, or private info visible in your screenshots end up in a
> searchable local file. Keep it on an encrypted disk and mind who has access.

---

## Back up your database (`backup_db.sh`)

The DB is the one irreplaceable artifact here — re-running OCR on a big library
costs hours (and API spend). `backup_db.sh` snapshots it to a git repo on a
schedule.

Two things it gets right that a naive `git add screenshots.db` does not:

- **Consistent snapshots.** It uses the sqlite3 `.backup` command, which copies a
  clean, openable database even while a backfill is actively writing. Plain `cp`
  of a live SQLite file can capture a torn, corrupt copy.
- **Privacy.** The DB holds OCR'd text in cleartext (see the warning above), so
  the backup repo **must be private**. The script requires you to point it at a
  remote — make sure that remote is a **private** repo.

```bash
# 1. Create a PRIVATE backup repo (GitHub example)
gh repo create screenshots-db-backup --private

# 2. Point the script at it and run
export BACKUP_REPO=git@github.com:you/screenshots-db-backup.git   # PRIVATE
./backup_db.sh
```

It clones the backup repo once (to `~/.cache/screenshot-backup`), snapshots every
valid SQLite `.db` in your `SCREENSHOT_DIGEST_HOME`, gzips each, and commits +
pushes only when something changed. Re-running just overwrites with a fresh
snapshot — safe anytime, including mid-backfill.

Schedule it nightly with cron:

```cron
# every night at 3am — back up the DB to the private repo
0 3 * * *  BACKUP_REPO=git@github.com:you/screenshots-db-backup.git /path/to/backup_db.sh >> ~/screenshot-backup.log 2>&1
```

| Env | Default | Purpose |
|-----|---------|---------|
| `BACKUP_REPO` | *(required)* | git remote of a **private** backup repo |
| `DIGEST_HOME` | `$SCREENSHOT_DIGEST_HOME` or `~/.screenshot-digest` | where the DB lives |
| `BACKUP_DIR` | `~/.cache/screenshot-backup` | local working clone of the backup repo |

---

## License

MIT — see [LICENSE](LICENSE).
