#!/usr/bin/env python3
"""
screenshot_viewer.py — elegant localhost browser for the screenshot-digest DB.

Run:
    python3 screenshot_viewer.py            # serves http://127.0.0.1:8765
    python3 screenshot_viewer.py --port 9000

Features:
    - Visual grid of every screenshot (thumbnails served from disk).
    - Live full-text filter across OCR text, summary, filename, category.
    - Filter by category + flag (keep/review/delete).
    - Click any shot to see the full image + full OCR text, and recategorize /
      re-flag it inline (persists straight to the SQLite DB).
    - Optional "Send to bot" — hand a screenshot + an instruction to an AI
      assistant that can actually take action (see "Bot actions" below).

Reads the same DB as screenshot_digest.py. Override location with
SCREENSHOT_DIGEST_HOME (defaults to ~/.screenshot-digest).

──────────────────────────────────────────────────────────────────────────
Bot actions (optional, off by default)
──────────────────────────────────────────────────────────────────────────
Each screenshot can be dispatched to an AI assistant with a free-text
instruction (e.g. "add this event to my calendar", "find ticket prices").
The viewer sends the text you choose (metadata / OCR / summary / image) plus
your instruction. Two ways to wire it up:

1. Generic command (any assistant / script). Set SCREENSHOT_ACTION_CMD to a
   shell command; the prompt is passed on stdin, or substituted for the
   literal token {prompt} if present:

       export SCREENSHOT_BOT_NAME="my assistant"
       export SCREENSHOT_ACTION_CMD='my-cli chat --stdin'
       # or:  export SCREENSHOT_ACTION_CMD='my-cli ask "{prompt}"'

2. OpenClaw (https://openclaw.ai) — the reference integration. It runs a real
   agent turn (with tools: calendar, web, email) and delivers the reply to a
   chat channel. Just point it at a target:

       export SCREENSHOT_BOT_NAME="Pal"
       export SCREENSHOT_ACTION_CHANNEL="telegram"   # default
       export SCREENSHOT_ACTION_TARGET="<your chat id>"
       export SCREENSHOT_ACTION_AGENT="main"         # default

Actions stay disabled until one of these is configured, so the panel is
hidden by default.
"""

import argparse
import io
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

HOME = Path(os.environ.get("SCREENSHOT_DIGEST_HOME", str(Path.home() / ".screenshot-digest")))
DB_PATH = HOME / "screenshots.db"
ACTION_LOG = HOME / "viewer_actions.log"

# What to call the assistant in the UI.
BOT_NAME = os.environ.get("SCREENSHOT_BOT_NAME", "your bot")

# Generic dispatch: any CLI/script. Prompt goes on stdin, or replaces {prompt}.
ACTION_CMD = os.environ.get("SCREENSHOT_ACTION_CMD", "").strip()

# OpenClaw reference integration (used when ACTION_CMD is not set).
OPENCLAW_BIN = os.environ.get("OPENCLAW_BIN", shutil.which("openclaw") or "openclaw")
ACTION_CHANNEL = os.environ.get("SCREENSHOT_ACTION_CHANNEL", "telegram")
ACTION_TARGET = os.environ.get("SCREENSHOT_ACTION_TARGET", "").strip()
ACTION_AGENT = os.environ.get("SCREENSHOT_ACTION_AGENT", "main")

_openclaw_ready = bool(ACTION_TARGET) and shutil.which(OPENCLAW_BIN) is not None
ACTION_ENABLED = bool(ACTION_CMD) or _openclaw_ready

CATEGORIES = [
    "article_text", "calendar_event", "code_snippet", "contact_info",
    "conversation", "document", "misc", "photo_media", "product_ui",
    "receipt_financial", "social_media", "url_link", "map_location",
]

# Single triage lifecycle (replaces the old keep/review/delete flag). Every shot
# is in exactly one state; acting on it (or sending it to the bot) moves it to
# "reviewed", and "archived" is the gentle replacement for "delete".
STATUSES = ["needs_review", "reviewed", "archived"]
STATUS_LABELS = {"needs_review": "Needs review", "reviewed": "Reviewed", "archived": "Archived"}

# User-managed quick messages (reusable instructions). Lives next to the DB so
# it's editable and portable; NOT hardcoded in the UI.
QMSG_PATH = HOME / "quick_messages.json"

# Optional Pillow for fast thumbnails; falls back to serving the raw file.
try:
    from PIL import Image
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False


def _content_disposition(filename):
    """Build a Content-Disposition header safe for ANY filename.

    HTTP headers must be Latin-1, but macOS screenshot names contain U+202F
    (narrow no-break space) and other non-Latin-1 chars. So we emit an ASCII
    fallback `filename=` plus an RFC 5987 `filename*=UTF-8''…` that modern
    browsers prefer — covering the full original name without crashing the
    header encoder.
    """
    from urllib.parse import quote
    ascii_name = filename.encode("ascii", "replace").decode("ascii").replace('"', "")
    return (f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quote(filename)}")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema():
    """Auto-migrate to the single `status` triage column. Idempotent.

    Replaces the old two-axis model (keep/review/delete `flag` + a `reviewed`
    boolean) with one lifecycle: needs_review → reviewed, plus archived. On the
    first run we backfill from whatever older columns exist so nothing is lost:
    reviewed=1 → reviewed, flag='delete' → archived, everything else → needs_review.
    """
    conn = db()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(screenshots)")}
    if "status" not in cols:
        conn.execute("ALTER TABLE screenshots ADD COLUMN status TEXT DEFAULT 'needs_review'")
        # Backfill from the legacy columns (guarding for DBs that lack them).
        if "reviewed" in cols:
            conn.execute("UPDATE screenshots SET status='reviewed' WHERE COALESCE(reviewed,0)=1")
        if "flag" in cols:
            conn.execute("UPDATE screenshots SET status='archived' WHERE flag='delete'")
        conn.execute("UPDATE screenshots SET status='needs_review' WHERE status IS NULL OR status=''")
        conn.commit()
    conn.close()


def load_quick_messages():
    try:
        data = json.loads(QMSG_PATH.read_text())
        return [str(x) for x in data if str(x).strip()] if isinstance(data, list) else []
    except Exception:
        return []


def save_quick_messages(msgs):
    seen, clean = set(), []
    for m in msgs:
        m = (m or "").strip()
        if m and m not in seen:
            seen.add(m)
            clean.append(m)
    QMSG_PATH.write_text(json.dumps(clean, indent=2))
    return clean


def fetch_all():
    conn = db()
    rows = conn.execute(
        "SELECT uuid, path, filename, date_taken, date_added, date_processed, "
        "ocr_text, category, summary, source, "
        "COALESCE(NULLIF(status,''), 'needs_review') AS status FROM screenshots "
        "ORDER BY COALESCE(date_taken, date_added) DESC"
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["ocr_len"] = len(d.get("ocr_text") or "")
        d["exists"] = bool(d["path"] and os.path.exists(d["path"]))
        d["status"] = d.get("status") or "needs_review"
        out.append(d)
    return out


def _build_prompt(row, instruction, inc):
    parts = ["📸 Screenshot action (from the screenshot viewer).\n"]
    if inc["meta"]:
        parts.append(
            f"File: {row['filename']}  ·  category: {row['category']}  ·  "
            f"source: {row['source']}  ·  date: {row['date_taken']}")
    if inc["image"]:
        parts.append(f"Local image (open it to see it): {row['path']}")
    if inc["summary"] and row["summary"]:
        parts.append(f"Summary: {row['summary']}")
    if inc["ocr"]:
        ocr = (row["ocr_text"] or "")[:6000]
        parts.append(f"\n--- OCR TEXT ---\n{ocr}\n--- END OCR ---")
    parts.append(f"\nREQUESTED ACTION: {instruction}\n")
    parts.append("Do the work and reply with the result.")
    return "\n".join(parts)


def dispatch_action(uuid, instruction, include=None, ocr_override=None):
    """Hand a screenshot + instruction to the configured assistant.

    `include` picks which context to attach (keeps cost down — the image is
    the expensive part). Keys: image, ocr, summary, meta. Defaults: image OFF.
    `ocr_override`, when provided, is saved back to the DB (cleaned-up text wins)
    and used in the outgoing prompt. Runs detached so we don't block the HTTP
    response; output is logged so failures are debuggable, not swallowed.
    """
    inc = {"image": False, "ocr": True, "summary": True, "meta": True}
    if include:
        inc.update({k: bool(v) for k, v in include.items() if k in inc})
    conn = db()
    # Persist the edited OCR first so the prompt and the stored data agree.
    if ocr_override is not None:
        conn.execute("UPDATE screenshots SET ocr_text=? WHERE uuid=?", (ocr_override, uuid))
        conn.commit()
    row = conn.execute(
        "SELECT filename, path, category, summary, ocr_text, date_taken, source "
        "FROM screenshots WHERE uuid=?", (uuid,)).fetchone()
    conn.close()
    if not row:
        return False, "screenshot not found"
    prompt = _build_prompt(row, instruction, inc)

    if ACTION_CMD:
        if "{prompt}" in ACTION_CMD:
            cmd = shlex.split(ACTION_CMD.replace("{prompt}", prompt))
            stdin_data = None
        else:
            cmd = shlex.split(ACTION_CMD)
            stdin_data = prompt
    else:
        cmd = [OPENCLAW_BIN, "agent", "--agent", ACTION_AGENT, "--channel", ACTION_CHANNEL,
               "--deliver", "--reply-to", ACTION_TARGET, "-m", prompt]
        stdin_data = None

    try:
        logf = open(ACTION_LOG, "a")
        logf.write(f"\n===== {uuid} :: {instruction!r} =====\n")
        logf.flush()
        p = subprocess.Popen(
            cmd, stdin=(subprocess.PIPE if stdin_data is not None else None),
            stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
        if stdin_data is not None:
            p.stdin.write(stdin_data.encode("utf-8"))
            p.stdin.close()
        return True, "dispatched"
    except Exception as e:
        return False, str(e)


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Screenshot Digest</title>
<script>
  // Set the theme before first paint (no flash). Stored choice wins; else follow the OS.
  (function(){
    var t = localStorage.getItem('theme');
    if(!t) t = matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', t);
  })();
</script>
<style>
  /* ── Design tokens. Dark by default; [data-theme="light"] overrides.
     A no-flash inline script in <head> sets data-theme before paint. ── */
  :root, :root[data-theme="dark"] {
    --bg:#1b1b1b; --panel:#242424; --panel2:#2d2d2d; --line:#373735;
    --txt:#e9e9e7; --muted:#9a9a96; --accent:#2e9fff; --accent-fg:#ffffff;
    --keep:#4cb782; --review:#e0a13a; --archived:#8a8a86; --delete:#e5604d;
    --on-fill:#161616;
    --img-bg:#0e0e0e; --badge-bg:rgba(18,18,18,.74); --header-bg:rgba(27,27,27,.82);
    --overlay:rgba(0,0,0,.66); --focus-ring:#ffffff;
    --selbox-border:rgba(255,255,255,.65); --code-fg:#cfcfca;
    --shadow:0 1px 2px rgba(0,0,0,.3); --shadow-md:0 6px 20px rgba(0,0,0,.4);
    --shadow-lg:0 18px 48px rgba(0,0,0,.55); --accent-soft:rgba(46,159,255,.16);
  }
  :root[data-theme="light"] {
    --bg:#ffffff; --panel:#ffffff; --panel2:#f7f7f5; --line:#eceae6;
    --txt:#37352f; --muted:#9b9a97; --accent:#2383e2; --accent-fg:#ffffff;
    --keep:#2f9e6f; --review:#cc8b1a; --archived:#b6b4af; --delete:#e0552f;
    --on-fill:#ffffff;
    --img-bg:#f3f2f0; --badge-bg:rgba(255,255,255,.92); --header-bg:rgba(255,255,255,.82);
    --overlay:rgba(15,15,15,.42); --focus-ring:#37352f;
    --selbox-border:rgba(55,53,47,.4); --code-fg:#4a4843;
    --shadow:0 1px 2px rgba(15,15,15,.06); --shadow-md:0 4px 16px rgba(15,15,15,.1);
    --shadow-lg:0 16px 48px rgba(15,15,15,.16); --accent-soft:rgba(35,131,226,.1);
  }
  * { box-sizing:border-box; }
  body { margin:0; font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif;
         background:var(--bg); color:var(--txt); -webkit-font-smoothing:antialiased;
         transition:background .2s, color .2s; }
  header { position:sticky; top:0; z-index:20; background:var(--header-bg);
           backdrop-filter:blur(14px) saturate(1.4); border-bottom:1px solid var(--line);
           padding:12px 22px; display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
  h1 { font-size:15px; margin:0; font-weight:600; letter-spacing:-.01em; }
  h1 span { color:var(--muted); font-weight:400; }
  #search { flex:1; min-width:220px; background:var(--panel2); border:1px solid transparent;
            color:var(--txt); padding:8px 14px; border-radius:9px; font-size:14px; outline:none;
            transition:.15s; }
  #search::placeholder { color:var(--muted); }
  #search:focus { border-color:var(--accent); background:var(--panel); box-shadow:0 0 0 3px var(--accent-soft); }
  .chips { display:flex; gap:6px; flex-wrap:wrap; }
  .chip { padding:5px 11px; border-radius:8px; border:1px solid var(--line);
          background:transparent; color:var(--muted); cursor:pointer; font-size:12.5px; font-weight:500;
          user-select:none; transition:.12s; white-space:nowrap; }
  .chip:hover { color:var(--txt); background:var(--panel2); }
  .chip.on { background:var(--accent); border-color:var(--accent); color:var(--accent-fg); }
  .chip.st-needs_review.on{background:var(--review);border-color:var(--review);color:var(--on-fill)}
  .chip.st-reviewed.on{background:var(--keep);border-color:var(--keep);color:var(--on-fill)}
  .chip.st-archived.on{background:var(--archived);border-color:var(--archived);color:var(--on-fill)}
  #count { color:var(--muted); font-size:12px; margin-left:auto; font-variant-numeric:tabular-nums; }
  main { padding:20px 22px 90px; columns: var(--col, 220px); column-gap:16px; }
  #sizeToggle { display:flex; gap:2px; background:var(--panel2); padding:2px; border-radius:9px; }
  #sizeToggle button { padding:5px 10px; border-radius:7px; border:none;
                       background:transparent; color:var(--muted); cursor:pointer; font-size:12px; font-weight:500; }
  #sizeToggle button:hover { color:var(--txt); }
  #sizeToggle button.on { background:var(--panel); color:var(--txt); box-shadow:var(--shadow); }
  .card { break-inside:avoid; margin:0 0 16px; background:var(--panel); border:1px solid var(--line);
          border-radius:11px; overflow:hidden; cursor:pointer; transition:box-shadow .15s, transform .15s, border-color .15s;
          position:relative; box-shadow:var(--shadow); }
  .card:hover { box-shadow:var(--shadow-md); transform:translateY(-2px); }
  .card img { width:100%; display:block; background:var(--img-bg); }
  .card .meta { padding:10px 12px; }
  .card .sum { font-size:12.5px; color:var(--txt); margin:0 0 7px; max-height:3em; overflow:hidden; line-height:1.45; }
  .card .tags { display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
  .tag { font-size:10.5px; padding:2px 8px; border-radius:6px; background:var(--panel2);
         color:var(--muted); font-weight:500; }
  .dot { width:7px; height:7px; border-radius:50%; display:inline-block; }
  .dot.needs_review{background:var(--review)} .dot.reviewed{background:var(--keep)} .dot.archived{background:var(--archived)}
  .card.is-archived { opacity:.5; }
  /* status shows as a tinted left edge for fast scanning */
  .card.st-needs_review { border-left:3px solid var(--review); }
  .card.st-reviewed { border-left:3px solid var(--keep); }
  .card.st-archived { border-left:3px solid var(--archived); }
  .status-badge { position:absolute; top:9px; left:9px; z-index:2; font-size:10px; font-weight:600;
                  padding:3px 8px; border-radius:7px; box-shadow:var(--shadow); letter-spacing:.01em; }
  .status-badge.needs_review { background:var(--review); color:var(--on-fill); }
  .status-badge.archived { background:var(--archived); color:var(--on-fill); }
  /* source badge (iPhone / Desktop) — top-right, glassy */
  .src-badge { position:absolute; top:9px; right:9px; z-index:2; font-size:10px; font-weight:600;
               padding:3px 8px; border-radius:7px; background:var(--badge-bg);
               color:var(--txt); box-shadow:var(--shadow); backdrop-filter:blur(6px); }
  /* selection checkbox — click-to-select */
  .selbox { position:absolute; bottom:9px; right:9px; z-index:3; width:24px; height:24px;
            border-radius:7px; border:2px solid var(--selbox-border); background:var(--badge-bg);
            display:flex; align-items:center; justify-content:center; cursor:pointer;
            color:transparent; font-size:14px; font-weight:800; transition:.1s; backdrop-filter:blur(6px); }
  .selbox:hover { border-color:var(--accent); }
  .card.selected { border-color:var(--accent); box-shadow:0 0 0 2px var(--accent), var(--shadow-md); }
  .card.selected .selbox { background:var(--accent); border-color:var(--accent); color:var(--accent-fg); }
  .card.focused { outline:2.5px solid var(--focus-ring); outline-offset:2px; }
  .card.focused.selected { outline-color:var(--accent); }
  .card .date { font-size:10.5px; color:var(--muted); margin-top:5px; font-variant-numeric:tabular-nums; }
  /* theme toggle */
  #themeBtn { font-size:14px; cursor:pointer; padding:6px 9px; border:1px solid var(--line);
              border-radius:8px; background:var(--panel); color:var(--txt); line-height:1; }
  #themeBtn:hover { border-color:var(--accent); }
  /* keyboard shortcuts help overlay */
  #help { position:fixed; inset:0; background:var(--overlay); backdrop-filter:blur(3px); z-index:70; display:none;
          align-items:center; justify-content:center; padding:30px; }
  #help.on { display:flex; }
  #help .card2 { background:var(--panel); border:1px solid var(--line); border-radius:16px;
                 padding:26px 30px; max-width:560px; width:100%; box-shadow:var(--shadow-lg); }
  #help h3 { margin:0 0 14px; font-size:15px; font-weight:600; }
  #help .grp { color:var(--accent); font-size:11px; text-transform:uppercase; letter-spacing:.06em; margin:16px 0 6px; font-weight:600; }
  #help table { width:100%; border-collapse:collapse; }
  #help td { padding:4px 0; font-size:13px; color:var(--txt); vertical-align:top; }
  #help td.k { width:140px; }
  #help kbd { background:var(--panel2); border:1px solid var(--line); border-bottom-width:2px;
              border-radius:6px; padding:1px 7px; font:12px ui-monospace,Menlo,monospace; color:var(--txt); }
  #kbdHint { font-size:12px; color:var(--muted); cursor:pointer; padding:6px 10px; border:1px solid var(--line);
             border-radius:8px; background:var(--panel); font-weight:500; }
  #kbdHint:hover { color:var(--txt); border-color:var(--accent); }
  /* bulk action bar */
  #bulkbar { position:fixed; bottom:14px; left:50%; transform:translate(-50%,140%); z-index:40;
             transition:transform .2s cubic-bezier(.2,.7,.3,1); background:var(--panel);
             border:1px solid var(--line); border-radius:14px; padding:10px 12px; display:flex; gap:8px;
             align-items:center; flex-wrap:wrap; box-shadow:var(--shadow-lg); max-width:calc(100vw - 28px); }
  #bulkbar.on { transform:translate(-50%,0); }
  #bulkbar .selcount { font-weight:600; font-size:13px; padding:0 6px; font-variant-numeric:tabular-nums; }
  #bulkbar button { padding:8px 13px; border-radius:8px; border:1px solid var(--line);
                    background:var(--panel2); color:var(--txt); cursor:pointer; font-size:13px; font-weight:500; transition:.12s; }
  #bulkbar button:hover { border-color:var(--accent); }
  #bulkbar button.danger:hover { border-color:var(--delete); color:var(--delete); }
  #bulkbar button.primary { background:var(--accent); border-color:var(--accent); color:var(--accent-fg); }
  #bulkbar button.primary:hover { filter:brightness(1.06); }
  #bulkbar input { background:var(--bg); border:1px solid var(--line); color:var(--txt);
                   padding:8px 12px; border-radius:8px; font-size:13px; min-width:220px; flex:1; outline:none; }
  #bulkbar input:focus { border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-soft); }
  #bulkbar .spacer { flex:1; }
  .missing { padding:34px 12px; text-align:center; color:var(--muted); font-size:11px; background:var(--panel2); }
  /* modal */
  #overlay { position:fixed; inset:0; background:var(--overlay); backdrop-filter:blur(3px); z-index:50; display:none;
             align-items:center; justify-content:center; padding:30px; }
  #overlay.on { display:flex; }
  .modal { background:var(--panel); border:1px solid var(--line); border-radius:16px;
           max-width:1340px; width:100%; max-height:90vh; display:flex; overflow:hidden; box-shadow:var(--shadow-lg); }
  .modal .img-pane { flex:1.1; background:var(--img-bg); display:flex; align-items:center; justify-content:center;
                     overflow:auto; min-width:0; }
  .modal .img-pane img { max-width:100%; max-height:90vh; object-fit:contain; }
  .modal .info { flex:1; padding:24px; overflow-y:auto; display:flex; flex-direction:column; gap:18px;
                 min-width:320px; border-left:1px solid var(--line); }
  /* third panel: OCR gets its own full-height column so nothing has to scroll past it */
  .modal .ocr-pane { flex:1; padding:24px; display:flex; flex-direction:column; gap:8px;
                     min-width:300px; border-left:1px solid var(--line); }
  .modal .ocr-pane #mOcr { flex:1; min-height:0; max-height:none; }
  #saveOcrBtn { align-self:flex-start; padding:7px 14px; border-radius:8px; border:1px solid var(--line);
                background:var(--panel2); color:var(--txt); cursor:pointer; font-size:12.5px; font-weight:500; transition:.12s; }
  #saveOcrBtn:hover { border-color:var(--accent); }
  @media (max-width:980px) {
    .modal { flex-direction:column; max-height:92vh; overflow-y:auto; }
    .modal .info, .modal .ocr-pane { border-left:none; border-top:1px solid var(--line); }
    .modal .ocr-pane #mOcr { min-height:160px; }
  }
  .mnav { display:flex; align-items:center; gap:10px; }
  .mnav button { padding:5px 11px; border-radius:7px; border:1px solid var(--line);
                 background:var(--panel2); color:var(--txt); cursor:pointer; font-size:12px; font-weight:500; }
  .mnav button:hover { border-color:var(--accent); }
  .mnav #mPos { font-size:12px; color:var(--muted); font-variant-numeric:tabular-nums; }
  .mnav .advtoggle { margin-left:auto; display:flex; align-items:center; gap:5px;
                     font-size:11.5px; color:var(--muted); cursor:pointer; }
  .mnav .advtoggle input { accent-color:var(--accent); cursor:pointer; }
  .modal h2 { margin:0; font-size:15px; font-weight:600; letter-spacing:-.01em; }
  .modal .label { font-size:11px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin-bottom:7px; font-weight:600; }
  .modal .ocr { background:var(--bg); border:1px solid var(--line); border-radius:10px; padding:12px;
                font:12px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace; white-space:pre-wrap;
                max-height:240px; overflow:auto; color:var(--code-fg); }
  .modal select { background:var(--panel2); color:var(--txt); border:1px solid var(--line);
                  border-radius:8px; padding:9px 11px; font-size:13px; width:100%; outline:none; }
  .modal select:focus { border-color:var(--accent); }
  .segbtns { display:flex; gap:8px; }
  .segbtns button { flex:1; padding:9px; border-radius:8px; border:1px solid var(--line);
                    background:var(--panel2); color:var(--muted); cursor:pointer; font-size:12px; font-weight:500; transition:.12s; }
  .segbtns button:hover{ color:var(--txt); }
  .segbtns button.on.needs_review{background:var(--review);color:var(--on-fill);border-color:var(--review)}
  .segbtns button.on.reviewed{background:var(--keep);color:var(--on-fill);border-color:var(--keep)}
  .segbtns button.on.archived{background:var(--archived);color:var(--on-fill);border-color:var(--archived)}
  .close { position:absolute; top:18px; right:24px; font-size:30px; color:#fff; cursor:pointer;
           z-index:60; line-height:1; opacity:.65; }
  .close:hover{opacity:1}
  .saved { color:var(--keep); font-size:11px; opacity:0; transition:.2s; font-weight:600; }
  .saved.show{opacity:1}
  .small { color:var(--muted); font-size:11px; }
  .dl { display:inline-block; margin-top:6px; font-size:12px; color:var(--accent);
        text-decoration:none; cursor:pointer; font-weight:500; }
  .dl:hover { text-decoration:underline; }
  #actionBox { background:var(--panel2); border:1px solid var(--line); border-radius:12px; padding:16px; }
  .incl { display:flex; gap:14px; flex-wrap:wrap; margin-bottom:12px; font-size:12px; color:var(--muted); }
  .incl label { display:flex; align-items:center; gap:5px; cursor:pointer; }
  .incl input { accent-color:var(--accent); cursor:pointer; }
  .qmsgs { display:flex; gap:6px; flex-wrap:wrap; margin-bottom:10px; }
  .qchip { display:inline-flex; align-items:center; gap:6px; font-size:11px; padding:5px 9px;
           border-radius:7px; border:1px solid var(--line); background:var(--panel); color:var(--muted); }
  .qchip span { cursor:pointer; }
  .qchip span:hover{ color:var(--txt); }
  .qchip .x { opacity:.45; cursor:pointer; font-weight:700; }
  .qchip .x:hover{ opacity:1; color:var(--delete); }
  .qadd { font-size:11px; color:var(--accent); cursor:pointer; border:1px dashed var(--line);
          background:none; padding:5px 9px; border-radius:7px; }
  .qadd:hover{ border-color:var(--accent); }
  .qmsg-empty { font-size:11px; color:var(--muted); }
  #mOcr { width:100%; min-height:90px; max-height:240px; resize:vertical; background:var(--bg);
          border:1px solid var(--line); border-radius:10px; padding:12px; color:var(--code-fg);
          font:12px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace; outline:none; }
  #mOcr:focus{ border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-soft); }
  #mAction { width:100%; min-height:64px; resize:vertical; background:var(--panel); color:var(--txt);
             border:1px solid var(--line); border-radius:8px; padding:10px 12px; font-size:13px;
             font-family:inherit; outline:none; }
  #mAction:focus{ border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-soft); }
  #sendBtn { margin-top:10px; width:100%; padding:11px; border-radius:8px; border:none;
             background:var(--accent); color:var(--accent-fg); font-size:13px; font-weight:600; cursor:pointer; transition:.12s; }
  #sendBtn:hover{ filter:brightness(1.06); } #sendBtn:disabled{ opacity:.5; cursor:default; }
</style>
</head>
<body>
<header>
  <h1>📸 Screenshots <span id="total"></span></h1>
  <input id="search" placeholder="Filter by text, summary, filename…" autocomplete="off">
  <div class="chips" id="statusChips"></div>
  <div class="chips" id="sourceChips"></div>
  <div class="chips" id="catChips"></div>
  <button id="selectAllBtn" class="chip" onclick="selectAllShown()">☑ Select all</button>
  <div id="kbdHint" onclick="toggleHelp()" title="Keyboard shortcuts (press ?)">⌨ shortcuts</div>
  <button id="themeBtn" onclick="toggleTheme()" title="Toggle light / dark"></button>
  <div id="sizeToggle" title="Thumbnail size">
    <button data-w="140">S</button><button data-w="190">M</button>
    <button data-w="240">L</button><button data-w="320">XL</button>
  </div>
  <div id="count"></div>
</header>
<main id="grid"></main>

<div id="bulkbar">
  <span class="selcount" id="selCount">0 selected</span>
  <button onclick="clearSel()">Clear</button>
  <button onclick="selectAllShown()">Select all shown</button>
  <span class="spacer"></span>
  <button onclick="bulkStatus('needs_review')">↺ Needs review</button>
  <button onclick="bulkStatus('reviewed')">✓ Reviewed</button>
  <button class="danger" onclick="bulkStatus('archived')">🗄 Archive</button>
  <input id="bulkAction" placeholder="Instruction to send to bot for all selected…">
  <button class="primary" id="bulkSendBtn" onclick="bulkSend()">Send →</button>
</div>

<div id="help" onclick="if(event.target.id==='help')toggleHelp(false)">
  <div class="card2">
    <h3>⌨ Keyboard shortcuts</h3>
    <div class="grp">Grid</div>
    <table>
      <tr><td class="k"><kbd>j</kbd> <kbd>k</kbd> / <kbd>↑</kbd> <kbd>↓</kbd> <kbd>←</kbd> <kbd>→</kbd></td><td>Move focus (white ring)</td></tr>
      <tr><td class="k"><kbd>x</kbd> / <kbd>space</kbd></td><td>Select / deselect focused (auto-advances)</td></tr>
      <tr><td class="k"><kbd>⌘A</kbd> / <kbd>ctrl A</kbd></td><td>Select all shown</td></tr>
      <tr><td class="k"><kbd>a</kbd></td><td>Archive selection (or focused)</td></tr>
      <tr><td class="k"><kbd>r</kbd></td><td>Mark reviewed</td></tr>
      <tr><td class="k"><kbd>u</kbd></td><td>Back to needs-review</td></tr>
      <tr><td class="k"><kbd>s</kbd></td><td>Send to bot — jumps to the instruction box (<kbd>Enter</kbd> sends)</td></tr>
      <tr><td class="k"><kbd>o</kbd> / <kbd>Enter</kbd></td><td>Open focused</td></tr>
      <tr><td class="k"><kbd>g</kbd> / <kbd>G</kbd></td><td>First / last</td></tr>
      <tr><td class="k"><kbd>/</kbd></td><td>Search</td></tr>
      <tr><td class="k"><kbd>Esc</kbd></td><td>Clear selection</td></tr>
    </table>
    <div class="grp">Open shot</div>
    <table>
      <tr><td class="k"><kbd>j</kbd> <kbd>k</kbd> / arrows</td><td>Next / previous shot</td></tr>
      <tr><td class="k"><kbd>a</kbd> <kbd>r</kbd> <kbd>u</kbd></td><td>Set status</td></tr>
      <tr><td class="k"><kbd>⌘Enter</kbd></td><td>Send to bot (from the instruction box)</td></tr>
      <tr><td class="k"><kbd>Esc</kbd></td><td>Close</td></tr>
    </table>
  </div>
</div>

<div id="overlay">
  <div class="close" onclick="closeModal()">×</div>
  <div class="modal">
    <div class="img-pane"><img id="mImg" src=""></div>
    <div class="info">
      <div class="mnav">
        <button onclick="modalStep(-1)" title="Previous (k)">‹ Prev</button>
        <span id="mPos"></span>
        <button onclick="modalStep(1)" title="Next (j)">Next ›</button>
        <label class="advtoggle" title="After a status change or send, jump to the next shot">
          <input type="checkbox" id="autoAdv"> auto-advance</label>
      </div>
      <div><h2 id="mFile"></h2><div class="small" id="mDate"></div>
        <a id="mDownload" class="dl" download>⬇ Download original</a></div>
      <div><div class="label">Summary</div><div id="mSum"></div></div>
      <div>
        <div class="label">Category <span class="saved" id="savedCat">saved ✓</span></div>
        <select id="mCat" onchange="saveCat()"></select>
      </div>
      <div>
        <div class="label">Status <span class="saved" id="savedStatus">saved ✓</span></div>
        <div class="segbtns" id="mStatus"></div>
      </div>
      <div id="actionBox">
        <div class="label">⚡ Send to <span id="botLabel"></span> <span class="saved" id="savedAction">on it ✓</span></div>
        <div class="incl" id="incl">
          <label title="Filename, category, source, date — cheap"><input type="checkbox" id="incMeta" checked> metadata</label>
          <label title="Full transcribed text — cheap, usually enough"><input type="checkbox" id="incOcr" checked> OCR text</label>
          <label title="One-line summary — cheap"><input type="checkbox" id="incSummary" checked> summary</label>
          <label title="Sends the actual image — costs vision tokens, only if the bot must SEE it"><input type="checkbox" id="incImage"> 🖼 image (costly)</label>
        </div>
        <div class="qmsgs" id="qmsgs"></div>
        <textarea id="mAction" placeholder="What should the bot do with this? (e.g. add this event to my calendar and find ticket prices)"></textarea>
        <button id="sendBtn" onclick="sendAction()">Send →</button>
        <div class="small" id="actionNote"></div>
      </div>
    </div>
    <div class="ocr-pane">
      <div class="label">OCR text <span class="small">(editable)</span> <span class="saved" id="savedOcr">saved ✓</span></div>
      <textarea id="mOcr" placeholder="(no text)"></textarea>
      <button id="saveOcrBtn" onclick="saveOcr()">Save OCR</button>
    </div>
  </div>
</div>

<script>
let DATA = [], cur = null, QMSGS = [];
// status filter defaults to the "Needs review" queue — that's the inbox.
const state = { q:"", cats:new Set(), sources:new Set(), status:new Set(["needs_review"]) };
const selected = new Set();  // uuids picked for bulk actions
let shown = [];      // the currently-filtered items, in display order
let focusIdx = 0;    // keyboard focus cursor into `shown`

// Friendly source labels with an icon (DB stores "photos" / "desktop").
const SOURCE_LABELS = { photos:"📱 iPhone", desktop:"🖥 Desktop" };
function srcLabel(s){ return SOURCE_LABELS[s] || (s? ('· '+s) : ''); }
function fmtDate(iso){
  if(!iso) return '';
  const d = new Date(iso); if(isNaN(d)) return iso;
  return d.toLocaleString(undefined,{month:'short',day:'numeric',year:'numeric',hour:'numeric',minute:'2-digit'});
}

// The ONE place image URLs are built. uuids can contain spaces, colons, and
// U+202F (macOS screenshot names) — URLSearchParams encodes all of it safely,
// so don't hand-build /img URLs anywhere else.
function imgUrl(uuid, opts={}) {
  const p = new URLSearchParams({ id: uuid, ...opts });
  return '/img?' + p.toString();
}

async function load() {
  DATA = await (await fetch('/api/screenshots')).json();
  document.getElementById('total').textContent = '· ' + DATA.length;
  await loadQmsgs();
  buildChips();
  render();
  updateBulkBar();
}

function buildChips() {
  const sc = document.getElementById('statusChips');
  STATUSES.forEach(s => {
    const el = document.createElement('div');
    el.className = 'chip st-'+s + (state.status.has(s)?' on':'');
    el.textContent = STATUS_LABELS[s];
    el.onclick=()=>{ state.status.has(s)?state.status.delete(s):state.status.add(s); el.classList.toggle('on'); render(); };
    sc.appendChild(el);
  });
  const srcs = [...new Set(DATA.map(d=>d.source).filter(Boolean))].sort();
  const srcBox = document.getElementById('sourceChips');
  srcs.forEach(s => {
    const el = document.createElement('div');
    el.className='chip'; el.textContent=srcLabel(s);
    el.onclick=()=>{ state.sources.has(s)?state.sources.delete(s):state.sources.add(s); el.classList.toggle('on'); render(); };
    srcBox.appendChild(el);
  });
  const cats = [...new Set(DATA.map(d=>d.category).filter(Boolean))].sort();
  const cc = document.getElementById('catChips');
  cats.forEach(c => {
    const el = document.createElement('div');
    el.className='chip'; el.textContent=c;
    el.onclick=()=>{ state.cats.has(c)?state.cats.delete(c):state.cats.add(c); el.classList.toggle('on'); render(); };
    cc.appendChild(el);
  });
}

function match(d) {
  if (state.status.size && !state.status.has(d.status||'needs_review')) return false;
  if (state.sources.size && !state.sources.has(d.source)) return false;
  if (state.cats.size && !state.cats.has(d.category)) return false;
  if (state.q) {
    const hay = ((d.ocr_text||'')+' '+(d.summary||'')+' '+(d.filename||'')+' '+(d.category||'')).toLowerCase();
    if (!hay.includes(state.q)) return false;
  }
  return true;
}

function render() {
  const g = document.getElementById('grid');
  const items = DATA.filter(match);
  shown = items;
  if (focusIdx > items.length-1) focusIdx = items.length-1;
  if (focusIdx < 0) focusIdx = 0;
  document.getElementById('count').textContent = items.length + ' shown';
  g.innerHTML = '';
  items.forEach((d, i) => {
    const st = d.status || 'needs_review';
    const card = document.createElement('div');
    card.className='card st-'+st + (st==='archived'?' is-archived':'')
      + (selected.has(d.uuid)?' selected':'') + (i===focusIdx?' focused':'');
    card.onclick=()=>{ focusIdx=i; openModal(d); };
    const img = d.exists
      ? `<img loading="lazy" src="${imgUrl(d.uuid)}">`
      : `<div class="missing">image unavailable<br>${d.filename||''}</div>`;
    const badge = (st==='needs_review') ? '<div class="status-badge needs_review">needs review</div>'
                : (st==='archived') ? '<div class="status-badge archived">archived</div>' : '';
    const src = d.source ? `<div class="src-badge">${srcLabel(d.source)}</div>` : '';
    card.innerHTML = badge + src + img +
      `<div class="selbox" title="Select">✓</div>` +
      `<div class="meta">
        <p class="sum">${esc(d.summary||d.filename||'—')}</p>
        <div class="tags"><span class="dot ${st}"></span>
          <span class="tag">${d.category||'—'}</span>
          <span class="tag">${srcLabel(d.source)}</span></div>
        <div class="date">${fmtDate(d.date_taken||d.date_added)}</div></div>`;
    card.querySelector('.selbox').onclick = (e)=>{ e.stopPropagation(); toggleSel(d.uuid); };
    g.appendChild(card);
  });
}

// ---- multi-select + bulk actions ----
function toggleSel(uuid){
  selected.has(uuid) ? selected.delete(uuid) : selected.add(uuid);
  render(); updateBulkBar();
}
function clearSel(){ selected.clear(); render(); updateBulkBar(); }
function selectAllShown(){ DATA.filter(match).forEach(d=>selected.add(d.uuid)); render(); updateBulkBar(); }
function updateBulkBar(){
  const bar = document.getElementById('bulkbar');
  document.getElementById('selCount').textContent = selected.size + ' selected';
  bar.classList.toggle('on', selected.size>0);
  document.getElementById('bulkSendBtn').style.display = ACTION_ENABLED ? '' : 'none';
  document.getElementById('bulkAction').style.display = ACTION_ENABLED ? '' : 'none';
}
async function bulkStatus(s){
  const uuids = [...selected];
  if(!uuids.length) return;
  await fetch('/api/bulk', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({uuids, status:s})});
  DATA.forEach(d=>{ if(selected.has(d.uuid)) d.status=s; });
  selected.clear(); render(); updateBulkBar();
}
async function bulkSend(){
  const uuids = [...selected];
  const instruction = document.getElementById('bulkAction').value.trim();
  if(!uuids.length){ document.getElementById('bulkAction').blur(); return; }
  if(!instruction){ document.getElementById('bulkAction').focus(); return; }
  const btn = document.getElementById('bulkSendBtn'); btn.disabled=true; btn.textContent='Sending…';
  const include = { meta:true, ocr:true, summary:true, image:false };
  for(const uuid of uuids){
    await fetch('/api/action', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({uuid, instruction, include})});
  }
  DATA.forEach(d=>{ if(selected.has(d.uuid)) d.status='reviewed'; });
  document.getElementById('bulkAction').value='';
  btn.disabled=false; btn.textContent='Send →';
  selected.clear(); render(); updateBulkBar();
}

// ---- keyboard navigation ----
function scrollFocusIntoView(){ const el=document.querySelector('.card.focused'); if(el) el.scrollIntoView({block:'nearest'}); }
function moveFocus(delta){
  if(!shown.length) return;
  focusIdx = Math.max(0, Math.min(shown.length-1, focusIdx+delta));
  render(); scrollFocusIntoView();
}
function toggleFocusSel(){
  const d=shown[focusIdx]; if(!d) return;
  selected.has(d.uuid)?selected.delete(d.uuid):selected.add(d.uuid);
  if(focusIdx<shown.length-1) focusIdx++;     // auto-advance for fast runs
  render(); updateBulkBar(); scrollFocusIntoView();
}
function openFocused(){ if(shown[focusIdx]) openModal(shown[focusIdx]); }
// Status from the keyboard: act on the selection if any, else the focused card.
async function kbStatus(s){
  if(selected.size){ await bulkStatus(s); return; }
  const d=shown[focusIdx]; if(!d) return;
  await fetch('/api/bulk', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({uuids:[d.uuid], status:s})});
  d.status=s; render(); scrollFocusIntoView();   // item may leave the filtered view; next slides in
}
function focusSendInput(){
  if(!ACTION_ENABLED) return;
  if(!selected.size && shown[focusIdx]) selected.add(shown[focusIdx].uuid);  // give send a target
  render(); updateBulkBar();
  const inp=document.getElementById('bulkAction'); inp.focus(); inp.select();
}
function modalStep(d){ moveFocus(d); if(shown[focusIdx]) openModal(shown[focusIdx]); }
// After acting on the open shot, move to the next one. If the shot dropped out of
// the current filter (e.g. archived while viewing Needs review), the next item has
// already shifted into its slot; otherwise step forward by one.
let AUTO_ADV = localStorage.getItem('autoAdv') !== '0';   // default ON
function advanceModal(){
  if(!shown.length){ closeModal(); return; }
  const acted = cur && cur.uuid;
  if(shown[focusIdx] && shown[focusIdx].uuid===acted){
    if(focusIdx < shown.length-1) focusIdx++; else { closeModal(); return; }
  } else if(focusIdx > shown.length-1){
    focusIdx = shown.length-1;
  }
  if(shown[focusIdx]){ openModal(shown[focusIdx]); scrollFocusIntoView(); } else closeModal();
}
function toggleHelp(show){
  const h=document.getElementById('help');
  h.classList.toggle('on', show===undefined ? !h.classList.contains('on') : !!show);
}
function modalOpen(){ return document.getElementById('overlay').classList.contains('on'); }

function openModal(d) {
  cur = d;
  const idx = shown.indexOf(d); if(idx>=0) focusIdx = idx;   // keep nav/counter in sync
  document.getElementById('mPos').textContent = shown.length ? ((focusIdx+1)+' / '+shown.length) : '';
  document.getElementById('autoAdv').checked = AUTO_ADV;
  document.getElementById('mImg').src = d.exists ? imgUrl(d.uuid, {full:1}) : '';
  const dl = document.getElementById('mDownload');
  if (d.exists) { dl.style.display='inline-block'; dl.href=imgUrl(d.uuid, {full:1, download:1}); }
  else { dl.style.display='none'; }
  document.getElementById('mFile').textContent = d.filename || d.uuid;
  document.getElementById('mDate').textContent =
     (d.source||'') + (d.date_taken? ' · '+d.date_taken : '');
  document.getElementById('mSum').textContent = d.summary || '—';
  document.getElementById('mOcr').value = d.ocr_text || '';
  const sel = document.getElementById('mCat'); sel.innerHTML='';
  CATS.forEach(c => { const o=document.createElement('option'); o.value=c; o.textContent=c;
     if(c===d.category)o.selected=true; sel.appendChild(o); });
  if (d.category && !CATS.includes(d.category)) {
     const o=document.createElement('option'); o.value=d.category; o.textContent=d.category; o.selected=true; sel.appendChild(o);
  }
  renderStatusBtns();
  renderQmsgs();
  setupAction(d);
  document.getElementById('overlay').classList.add('on');
}
function closeModal(){ document.getElementById('overlay').classList.remove('on'); }

function renderStatusBtns() {
  const sb = document.getElementById('mStatus'); sb.innerHTML='';
  const st = cur.status || 'needs_review';
  STATUSES.forEach(s => {
    const b=document.createElement('button'); b.textContent=STATUS_LABELS[s];
    b.className=s+(s===st?' on':''); b.onclick=()=>saveStatus(s);
    sb.appendChild(b);
  });
}
async function saveStatus(s) {
  await update(cur.uuid, {status:s}); cur.status=s;
  renderStatusBtns(); flash('savedStatus'); render();
  if(AUTO_ADV) advanceModal();
}
async function saveCat() {
  const v = document.getElementById('mCat').value;
  await update(cur.uuid, {category:v}); cur.category=v;
  flash('savedCat'); render();
}
// Persist edited OCR without sending — overrides the auto OCR with your cleanup.
async function saveOcr() {
  const v = document.getElementById('mOcr').value;
  await update(cur.uuid, {ocr_text:v}); cur.ocr_text=v;
  flash('savedOcr');
}
async function update(uuid, fields) {
  await fetch('/api/update', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({uuid, ...fields})});
}
function flash(id){ const e=document.getElementById(id); e.classList.add('show'); setTimeout(()=>e.classList.remove('show'),1200); }
function esc(s){ return (s||'').replace(/[&<>]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

document.getElementById('search').addEventListener('input', e=>{ state.q=e.target.value.toLowerCase().trim(); focusIdx=0; render(); });
document.getElementById('overlay').addEventListener('click', e=>{ if(e.target.id==='overlay') closeModal(); });

document.addEventListener('keydown', e=>{
  const t = e.target;
  const typing = t && t.matches && t.matches('input,textarea,select');

  // Escape always wins: close modal/help, else clear selection, else blur a field.
  if(e.key==='Escape'){
    if(document.getElementById('help').classList.contains('on')) return toggleHelp(false);
    if(modalOpen()) return closeModal();
    if(typing) return t.blur();
    if(selected.size) return clearSel();
    return;
  }
  // While typing, only handle the dedicated send shortcuts; let everything else through.
  if(typing){
    if(t.id==='bulkAction' && e.key==='Enter'){ e.preventDefault(); bulkSend(); }
    else if(t.id==='mAction' && e.key==='Enter' && (e.metaKey||e.ctrlKey)){ e.preventDefault(); sendAction(); }
    return;
  }

  if(e.key==='?'){ e.preventDefault(); return toggleHelp(); }
  if(e.key==='/'){ e.preventDefault(); return document.getElementById('search').focus(); }
  if((e.metaKey||e.ctrlKey) && (e.key==='a'||e.key==='A')){ e.preventDefault(); return selectAllShown(); }
  if(e.metaKey||e.ctrlKey||e.altKey) return;  // don't shadow browser/OS combos

  if(modalOpen()){
    if(e.key==='j'||e.key==='ArrowDown'||e.key==='ArrowRight'){ e.preventDefault(); modalStep(1); }
    else if(e.key==='k'||e.key==='ArrowUp'||e.key==='ArrowLeft'){ e.preventDefault(); modalStep(-1); }
    else if(e.key==='a'){ saveStatus('archived'); }
    else if(e.key==='r'){ saveStatus('reviewed'); }
    else if(e.key==='u'){ saveStatus('needs_review'); }
    return;
  }

  switch(e.key){
    case 'j': case 'ArrowDown': case 'l': case 'ArrowRight': e.preventDefault(); moveFocus(1); break;
    case 'k': case 'ArrowUp': case 'h': case 'ArrowLeft': e.preventDefault(); moveFocus(-1); break;
    case 'x': case ' ': e.preventDefault(); toggleFocusSel(); break;
    case 'o': case 'Enter': e.preventDefault(); openFocused(); break;
    case 'a': kbStatus('archived'); break;
    case 'r': kbStatus('reviewed'); break;
    case 'u': kbStatus('needs_review'); break;
    case 's': e.preventDefault(); focusSendInput(); break;
    case 'g': focusIdx=0; moveFocus(0); break;
    case 'G': focusIdx=shown.length-1; moveFocus(0); break;
  }
});

const CATS = __CATS__;
const STATUS_LABELS = __STATUS_LABELS__;
const STATUSES = Object.keys(STATUS_LABELS);
const ACTION_ENABLED = __ACTION_ENABLED__;
const BOT_NAME = __BOT_NAME__;

// ---- Quick messages (user-managed, pulled from the server; not hardcoded) ----
async function loadQmsgs() {
  try { QMSGS = (await (await fetch('/api/quickmsgs')).json()).messages || []; }
  catch { QMSGS = []; }
}
function renderQmsgs() {
  const box = document.getElementById('qmsgs'); box.innerHTML='';
  QMSGS.forEach(t => {
    const chip=document.createElement('div'); chip.className='qchip';
    const lbl=document.createElement('span'); lbl.textContent=t;
    lbl.onclick=()=>{ const a=document.getElementById('mAction'); a.value=(a.value?a.value+' ':'')+t; a.focus(); };
    const x=document.createElement('span'); x.className='x'; x.textContent='×'; x.title='Remove';
    x.onclick=(e)=>{ e.stopPropagation(); delQmsg(t); };
    chip.append(lbl, x); box.appendChild(chip);
  });
  const add=document.createElement('button'); add.className='qadd'; add.textContent='+ save current as quick message';
  add.onclick=saveCurrentAsQmsg; box.appendChild(add);
  if (!QMSGS.length) { const e=document.createElement('span'); e.className='qmsg-empty';
    e.textContent='No quick messages yet — type one below, then “+ save”.'; box.insertBefore(e, add); }
}
async function postQmsg(payload) {
  QMSGS = (await (await fetch('/api/quickmsgs', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)})).json()).messages || [];
  renderQmsgs();
}
async function saveCurrentAsQmsg() {
  const t = document.getElementById('mAction').value.trim();
  if (!t) { document.getElementById('mAction').focus(); return; }
  await postQmsg({text:t});
}
async function delQmsg(t) { await postQmsg({op:'delete', text:t}); }

function setupAction(d) {
  const box = document.getElementById('actionBox');
  if (!ACTION_ENABLED) { box.style.display='none'; return; }
  box.style.display='block';
  document.getElementById('botLabel').textContent = BOT_NAME;
  document.getElementById('sendBtn').textContent = 'Send to '+BOT_NAME+' →';
  document.getElementById('mAction').value = '';
  document.getElementById('actionNote').textContent = '';
}

async function sendAction() {
  const instruction = document.getElementById('mAction').value.trim();
  if (!instruction) { document.getElementById('mAction').focus(); return; }
  const btn = document.getElementById('sendBtn'); btn.disabled=true; btn.textContent='Sending…';
  const include = {
    meta: document.getElementById('incMeta').checked,
    ocr: document.getElementById('incOcr').checked,
    summary: document.getElementById('incSummary').checked,
    image: document.getElementById('incImage').checked,
  };
  // Send the (possibly edited) OCR — the server saves it back, cleaning the data.
  const ocr = document.getElementById('mOcr').value;
  const r = await (await fetch('/api/action', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({uuid:cur.uuid, instruction, include, ocr})})).json();
  btn.disabled=false; btn.textContent='Send to '+BOT_NAME+' →';
  if (r.ok) { flash('savedAction'); flash('savedOcr');
    cur.ocr_text = ocr; cur.status = 'reviewed'; renderStatusBtns(); render();  // sending counts as reviewing
    document.getElementById('actionNote').textContent=BOT_NAME+' is on it — marked reviewed, OCR saved. Watch your chat for the reply.';
    if(AUTO_ADV) setTimeout(advanceModal, 700);   // brief pause so the confirmation is visible
  } else { document.getElementById('actionNote').textContent='⚠ '+(r.error||r.msg||'failed'); }
}

// ---- auto-advance toggle (persisted) ----
document.getElementById('autoAdv').addEventListener('change', e=>{
  AUTO_ADV = e.target.checked; localStorage.setItem('autoAdv', AUTO_ADV ? '1' : '0');
});

// ---- theme toggle (persisted; defaults to OS) ----
function applyThemeGlyph(){
  const dark = document.documentElement.getAttribute('data-theme') !== 'light';
  document.getElementById('themeBtn').textContent = dark ? '☀️' : '🌙';
}
function toggleTheme(){
  const dark = document.documentElement.getAttribute('data-theme') !== 'light';
  const next = dark ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('theme', next);
  applyThemeGlyph();
}
applyThemeGlyph();

// ---- thumbnail size toggle (persisted) ----
function setSize(w){
  document.documentElement.style.setProperty('--col', w+'px');
  localStorage.setItem('shotColW', w);
  document.querySelectorAll('#sizeToggle button').forEach(b=>b.classList.toggle('on', b.dataset.w===String(w)));
}
document.querySelectorAll('#sizeToggle button').forEach(b=> b.onclick=()=>setSize(b.dataset.w));
setSize(localStorage.getItem('shotColW') || '190');

load();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            html = (INDEX_HTML
                    .replace("__CATS__", json.dumps(CATEGORIES))
                    .replace("__STATUS_LABELS__", json.dumps(STATUS_LABELS))
                    .replace("__ACTION_ENABLED__", "true" if ACTION_ENABLED else "false")
                    .replace("__BOT_NAME__", json.dumps(BOT_NAME)))
            return self._send(200, html, "text/html; charset=utf-8")
        if u.path == "/api/screenshots":
            return self._send(200, json.dumps(fetch_all()))
        if u.path == "/api/quickmsgs":
            return self._send(200, json.dumps({"messages": load_quick_messages()}))
        if u.path == "/img":
            # uuid travels as a query param (?id=), NOT a path segment: desktop
            # uuids are "desktop:<filename>" and macOS screenshot names contain a
            # colon and a narrow no-break space (U+202F). parse_qs decodes the
            # percent-encoding for us, so there's no manual unquote to forget and
            # no path-splitting to trip over. Client must encodeURIComponent the id.
            qs = parse_qs(u.query)
            uuid = (qs.get("id") or [""])[0]
            if not uuid:
                return self._send(400, json.dumps({"error": "missing id"}))
            return self._serve_image(uuid, qs)
        # Back-compat: older /img/<uuid> path form (still decode defensively).
        if u.path.startswith("/img/"):
            return self._serve_image(unquote(u.path[len("/img/"):]), parse_qs(u.query))
        return self._send(404, json.dumps({"error": "not found"}))

    def _serve_image(self, uuid, qs):
        conn = db()
        row = conn.execute(
            "SELECT path, filename FROM screenshots WHERE uuid=?", (uuid,)).fetchone()
        conn.close()
        if not row or not row["path"] or not os.path.exists(row["path"]):
            return self._send(404, b"", "image/png")
        path = row["path"]
        full = "full" in qs          # native resolution (modal / download)
        download = "download" in qs  # force a file download
        ext = os.path.splitext(path)[1].lower()
        ctype = "image/png" if ext == ".png" else "image/jpeg"

        # Grid thumbnails (downscaled) keep big libraries snappy; the modal and
        # the download link ask for ?full=1 to get the file at native resolution.
        if not full and HAVE_PIL:
            try:
                im = Image.open(path)
                im.thumbnail((900, 900))
                buf = io.BytesIO()
                im.convert("RGB").save(buf, "JPEG", quality=82)
                return self._image_bytes(buf.getvalue(), "image/jpeg")
            except Exception:
                pass
        with open(path, "rb") as f:
            data = f.read()
        # Name the download after the original filename, but with the extension
        # of the file we actually have on disk — for iCloud "optimized" Photos
        # that's a cached JPEG derivative, not the original PNG, so don't lie
        # about the type.
        stem = os.path.splitext(row["filename"] or uuid)[0]
        name = stem + ext
        disp = _content_disposition(name) if download else None
        self._image_bytes(data, ctype, disposition=disp)

    def _image_bytes(self, data, ctype, disposition=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "max-age=86400")
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path not in ("/api/update", "/api/action", "/api/quickmsgs", "/api/bulk"):
            return self._send(404, json.dumps({"error": "not found"}))
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or "{}")

        # Bulk status update for multi-select (archive / reviewed / needs_review).
        if u.path == "/api/bulk":
            uuids = body.get("uuids") or []
            status = body.get("status")
            if not uuids or status not in STATUSES:
                return self._send(400, json.dumps({"error": "uuids and a valid status required"}))
            conn = db()
            conn.executemany("UPDATE screenshots SET status=? WHERE uuid=?",
                             [(status, u_) for u_ in uuids])
            conn.commit()
            conn.close()
            return self._send(200, json.dumps({"ok": True, "updated": len(uuids), "status": status}))

        # Quick-message list management (no uuid needed).
        if u.path == "/api/quickmsgs":
            msgs = load_quick_messages()
            text = (body.get("text") or "").strip()
            if body.get("op") == "delete":
                msgs = [m for m in msgs if m != text]
            elif text:
                msgs.append(text)
            return self._send(200, json.dumps({"messages": save_quick_messages(msgs)}))

        uuid = body.get("uuid")
        if not uuid:
            return self._send(400, json.dumps({"error": "uuid required"}))

        if u.path == "/api/action":
            if not ACTION_ENABLED:
                return self._send(200, json.dumps(
                    {"ok": False, "error": "actions disabled (set SCREENSHOT_ACTION_CMD or OpenClaw target)"}))
            instruction = (body.get("instruction") or "").strip()
            if not instruction:
                return self._send(400, json.dumps({"error": "instruction required"}))
            # Optional edited OCR — saved back so the data gets cleaned up over time.
            ocr_override = body.get("ocr")
            ok, msg = dispatch_action(uuid, instruction, body.get("include"), ocr_override=ocr_override)
            if ok:
                # Acting on a shot counts as reviewing it — clear it from the queue.
                conn = db()
                conn.execute("UPDATE screenshots SET status='reviewed' WHERE uuid=?", (uuid,))
                conn.commit()
                conn.close()
            return self._send(200, json.dumps({"ok": ok, "msg": msg, "status": "reviewed" if ok else None}))

        sets, vals = [], []
        for col in ("category", "summary", "status", "ocr_text"):
            if col in body:
                sets.append(f"{col}=?")
                vals.append(body[col])
        if not sets:
            return self._send(400, json.dumps({"error": "nothing to update"}))
        vals.append(uuid)
        conn = db()
        conn.execute(f"UPDATE screenshots SET {', '.join(sets)} WHERE uuid=?", vals)
        conn.commit()
        conn.close()
        self._send(200, json.dumps({"ok": True}))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    if not DB_PATH.exists():
        sys.exit(f"DB not found at {DB_PATH} (run screenshot_digest.py first, "
                 f"or set SCREENSHOT_DIGEST_HOME)")
    ensure_schema()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"[viewer] {DB_PATH}")
    print(f"[viewer] serving {url}  (Pillow thumbnails: {HAVE_PIL}; "
          f"bot actions: {'on' if ACTION_ENABLED else 'off'})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
