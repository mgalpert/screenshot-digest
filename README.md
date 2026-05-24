# 📸 screenshot-digest

Turn your screenshot graveyard into a daily, searchable, auto-categorized review.

`screenshot-digest` finds your screenshots (macOS **Photos** library + **Desktop**),
reads the text inside them (OCR), classifies each one, flags it **keep / review /
delete**, and produces a clean daily markdown report — so you can actually decide
what to act on and what to delete, instead of scrolling through thousands of images.

Everything runs locally except the optional Gemini call, and **no images are ever
uploaded unless you enable the Gemini engine** (which sends the image to Google's
API for OCR).

---

## Why

If you screenshot a lot, your library becomes a write-only memory hole. This tool
makes it reviewable:

- **OCR** every screenshot so its contents are searchable text.
- **Categorize** into contacts, receipts, code, links, calendar events, articles,
  conversations, maps, social, app UI, documents, etc.
- **Flag** what's worth keeping vs. safe to delete.
- **Daily digest** grouped by action and category.

---

## How it works

```
DISCOVERY → OCR + CLASSIFY → CACHE (SQLite) → DAILY DIGEST (markdown)
 Photos +     Gemini Flash       dedupe by        grouped by
 Desktop      (or local OCR)     image uuid       keep/review/delete
```

**Two engines:**

| Engine | OCR quality | Cost | Privacy |
|--------|-------------|------|---------|
| **Gemini Flash** (default) | Best — handles infographics, dark UI, dense tables | ~$0.0004/screenshot | Image sent to Google API |
| **Local** (`--local`) | Good on plain text; weaker on complex images | Free | 100% on-device |

In testing, Gemini decisively beat local OCR (Apple Vision / Tesseract) on visually
complex screenshots where Tesseract failed outright and Vision garbled numbers. For
plain-text screenshots they're equivalent. Use `--local` if you want zero data to
leave your machine.

> **iCloud note:** if your Photos use "Optimize Mac Storage," originals live in
> iCloud. The tool automatically uses the locally-cached preview (plenty for OCR)
> — no downloads required.

---

## Install

Requires **macOS** and **Python 3.10+**.

```bash
git clone https://github.com/mgalpert/screenshot-digest.git
cd screenshot-digest
pip install -r requirements.txt        # osxphotos + ocrmac
brew install tesseract                 # optional local OCR fallback
```

Grant your terminal **Photos** (and/or **Full Disk**) access in
System Settings → Privacy & Security so it can read the Photos library.

### Gemini key (for the default engine)

Get a key at https://aistudio.google.com/apikey, then either export it or drop a
`.env` next to the script:

```bash
export GEMINI_OCR_KEY=your_key_here
# or:  echo 'GEMINI_OCR_KEY=your_key_here' > .env
```

No key? It automatically falls back to local OCR (same as `--local`).

---

## Usage

```bash
# Daily digest — new screenshots from the last 24h (Photos)
python3 screenshot_digest.py --report

# Include Desktop screenshots
python3 screenshot_digest.py --desktop --report

# Look back further
python3 screenshot_digest.py --days 7 --desktop --report

# Reprocess everything
python3 screenshot_digest.py --all --desktop --report

# Force fully-local (no API, nothing leaves your machine)
python3 screenshot_digest.py --local --report

# Inspect stored OCR text for a screenshot
python3 screenshot_digest.py --show "receipt"
```

| Flag | Effect |
|------|--------|
| `--days N` | Lookback window in days (default 1) |
| `--all` | Reprocess everything, ignore cache |
| `--desktop` | Also scan `~/Desktop/Screenshot*.png` |
| `--report` | Write a markdown report instead of printing |
| `--local` | Force offline (Apple Vision OCR + rules), skip Gemini |
| `--show SUBSTR` | Dump stored OCR text for filenames matching SUBSTR |

Output goes to `~/.screenshot-digest/` by default (DB + reports). Override with
`SCREENSHOT_DIGEST_HOME`. Pick the model with `SCREENSHOT_GEMINI_MODEL`
(default `gemini-3.5-flash`).

---

## Automate (daily report)

Run it nightly with `cron`:

```cron
5 20 * * *  /usr/bin/python3 /path/to/screenshot_digest.py --desktop --report
```

---

## Visual viewer (browse · filter · recategorize · act)

The digest is great for a daily glance, but sometimes you want to *see* your
screenshots. `screenshot_viewer.py` is a self-contained localhost web app
(Python stdlib only) that reads the same SQLite DB:

```bash
python3 screenshot_viewer.py            # → http://127.0.0.1:8765
python3 screenshot_viewer.py --port 9000
```

- **Visual grid** of every screenshot (thumbnails served straight from disk).
- **Live text filter** across OCR text, summary, filename, and category.
- **Filter chips** for category and triage status.
- **Triage lifecycle.** Every shot is in one of three states — **Needs review →
  Reviewed → Archived**. New shots land in *Needs review* (badged in the grid),
  and the view defaults to that queue so it works like an inbox. Acting on a shot
  — sending it to the bot, or picking a status in the modal — moves it along;
  *Archived* is the gentle "done with this" state (dimmed, not deleted).
- **Click any shot** for the full image + **editable OCR text** and inline
  **recategorize / restatus** — changes save straight back to the DB. Editing the
  OCR and hitting send overwrites the stored text, so you clean up the data as
  you go.

> Thumbnails are snappier if you have Pillow (`pip install pillow`); without it
> the viewer just serves the raw images.

### "Send to bot" actions (optional, off by default)

Each screenshot can be handed to an AI assistant with a free-text instruction —
*"add this event to my calendar,"* *"find ticket prices,"* *"draft a reply."*
You choose, via checkboxes, exactly what to send (metadata / OCR text / summary
/ the image itself). The image is the only part that costs vision tokens, so
it's **off by default** — the cheap text usually has everything.

Wire it to any assistant two ways:

```bash
# A) Any CLI/script — prompt is piped on stdin, or replaces {prompt}
export SCREENSHOT_BOT_NAME="my assistant"
export SCREENSHOT_ACTION_CMD='my-cli chat --stdin'

# B) OpenClaw (https://openclaw.ai) — runs a real agent turn with tools
#    (calendar, web, email) and delivers the reply to a chat channel
export SCREENSHOT_BOT_NAME="Pal"
export SCREENSHOT_ACTION_TARGET="<your chat id>"
export SCREENSHOT_ACTION_CHANNEL="telegram"   # default
```

The action panel stays hidden until one of these is configured.

**Quick messages.** Instead of hardcoded prompts, the panel shows *your* reusable
instructions. Type one and hit **+ save current as quick message**; it's stored
in `quick_messages.json` next to the DB and shown as a one-click chip on every
shot (the `×` removes it). Starts empty — it fills with whatever you actually use.

---

## The data is yours

Results live in a plain SQLite DB you can query:

```bash
sqlite3 ~/.screenshot-digest/screenshots.db \
  "SELECT filename, summary FROM screenshots WHERE category='receipt_financial';"
```

> ⚠️ **Privacy heads-up:** the DB stores OCR'd text in cleartext — that means any
> passwords, 2FA codes, or private info visible in your screenshots end up in a
> searchable local file. Keep it on an encrypted disk and mind who has access.

---

## License

MIT — see [LICENSE](LICENSE).
