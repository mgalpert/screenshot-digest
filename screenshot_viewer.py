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
from urllib.parse import urlparse, parse_qs

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
FLAGS = ["keep", "review", "delete"]

# Optional Pillow for fast thumbnails; falls back to serving the raw file.
try:
    from PIL import Image
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_all():
    conn = db()
    rows = conn.execute(
        "SELECT uuid, path, filename, date_taken, date_added, date_processed, "
        "ocr_text, category, flag, summary, source FROM screenshots "
        "ORDER BY COALESCE(date_taken, date_added) DESC"
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["ocr_len"] = len(d.get("ocr_text") or "")
        d["exists"] = bool(d["path"] and os.path.exists(d["path"]))
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


def dispatch_action(uuid, instruction, include=None):
    """Hand a screenshot + instruction to the configured assistant.

    `include` picks which context to attach (keeps cost down — the image is
    the expensive part). Keys: image, ocr, summary, meta. Defaults: image OFF.
    Runs detached so we don't block the HTTP response; output is logged so
    failures are debuggable, not swallowed.
    """
    inc = {"image": False, "ocr": True, "summary": True, "meta": True}
    if include:
        inc.update({k: bool(v) for k, v in include.items() if k in inc})
    conn = db()
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
<style>
  :root {
    --bg:#0d0f14; --panel:#161922; --panel2:#1d212c; --line:#2a2f3c;
    --txt:#e6e9ef; --muted:#8b93a7; --accent:#5b9dff; --keep:#3ecf8e;
    --review:#f5b14c; --delete:#ff6b6b;
  }
  * { box-sizing:border-box; }
  body { margin:0; font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
         background:var(--bg); color:var(--txt); }
  header { position:sticky; top:0; z-index:20; background:rgba(13,15,20,.92);
           backdrop-filter:blur(12px); border-bottom:1px solid var(--line);
           padding:14px 20px; display:flex; gap:14px; align-items:center; flex-wrap:wrap; }
  h1 { font-size:16px; margin:0; font-weight:650; letter-spacing:.3px; }
  h1 span { color:var(--muted); font-weight:400; }
  #search { flex:1; min-width:220px; background:var(--panel2); border:1px solid var(--line);
            color:var(--txt); padding:9px 14px; border-radius:10px; font-size:14px; outline:none; }
  #search:focus { border-color:var(--accent); }
  .chips { display:flex; gap:6px; flex-wrap:wrap; }
  .chip { padding:5px 11px; border-radius:999px; border:1px solid var(--line);
          background:var(--panel); color:var(--muted); cursor:pointer; font-size:12px;
          user-select:none; transition:.12s; white-space:nowrap; }
  .chip:hover { color:var(--txt); }
  .chip.on { background:var(--accent); border-color:var(--accent); color:#fff; }
  .chip.flag-keep.on{background:var(--keep);border-color:var(--keep)}
  .chip.flag-review.on{background:var(--review);border-color:var(--review);color:#1a1a1a}
  .chip.flag-delete.on{background:var(--delete);border-color:var(--delete)}
  #count { color:var(--muted); font-size:12px; margin-left:auto; }
  main { padding:18px; columns: 5 220px; column-gap:14px; }
  .card { break-inside:avoid; margin:0 0 14px; background:var(--panel); border:1px solid var(--line);
          border-radius:12px; overflow:hidden; cursor:pointer; transition:.15s; position:relative; }
  .card:hover { border-color:var(--accent); transform:translateY(-2px); }
  .card img { width:100%; display:block; background:#000; }
  .card .meta { padding:9px 11px; }
  .card .sum { font-size:12px; color:var(--txt); margin:0 0 6px; max-height:3em; overflow:hidden; }
  .card .tags { display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
  .tag { font-size:10px; padding:2px 7px; border-radius:6px; background:var(--panel2);
         color:var(--muted); }
  .dot { width:7px; height:7px; border-radius:50%; display:inline-block; }
  .dot.keep{background:var(--keep)} .dot.review{background:var(--review)} .dot.delete{background:var(--delete)}
  .missing { padding:30px 10px; text-align:center; color:var(--muted); font-size:11px; background:var(--panel2); }
  /* modal */
  #overlay { position:fixed; inset:0; background:rgba(0,0,0,.8); z-index:50; display:none;
             align-items:center; justify-content:center; padding:30px; }
  #overlay.on { display:flex; }
  .modal { background:var(--panel); border:1px solid var(--line); border-radius:16px;
           max-width:1100px; width:100%; max-height:90vh; display:flex; overflow:hidden; }
  .modal .img-pane { flex:1.2; background:#000; display:flex; align-items:center; justify-content:center;
                     overflow:auto; min-width:0; }
  .modal .img-pane img { max-width:100%; max-height:90vh; object-fit:contain; }
  .modal .info { flex:1; padding:22px; overflow-y:auto; display:flex; flex-direction:column; gap:16px; min-width:320px; }
  .modal h2 { margin:0; font-size:15px; }
  .modal .label { font-size:11px; text-transform:uppercase; letter-spacing:.6px; color:var(--muted); margin-bottom:6px; }
  .modal .ocr { background:var(--bg); border:1px solid var(--line); border-radius:10px; padding:12px;
                font:12px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace; white-space:pre-wrap;
                max-height:240px; overflow:auto; color:#c7ccd8; }
  .modal select { background:var(--panel2); color:var(--txt); border:1px solid var(--line);
                  border-radius:8px; padding:8px 10px; font-size:13px; width:100%; }
  .flagbtns { display:flex; gap:8px; }
  .flagbtns button { flex:1; padding:9px; border-radius:8px; border:1px solid var(--line);
                     background:var(--panel2); color:var(--muted); cursor:pointer; font-size:12px; }
  .flagbtns button.on.keep{background:var(--keep);color:#08130d;border-color:var(--keep)}
  .flagbtns button.on.review{background:var(--review);color:#1a1a1a;border-color:var(--review)}
  .flagbtns button.on.delete{background:var(--delete);color:#fff;border-color:var(--delete)}
  .close { position:absolute; top:18px; right:24px; font-size:26px; color:#fff; cursor:pointer;
           z-index:60; line-height:1; opacity:.7; }
  .close:hover{opacity:1}
  .saved { color:var(--keep); font-size:11px; opacity:0; transition:.2s; }
  .saved.show{opacity:1}
  .small { color:var(--muted); font-size:11px; }
  #actionBox { background:var(--bg); border:1px solid var(--line); border-radius:12px; padding:14px; }
  .incl { display:flex; gap:14px; flex-wrap:wrap; margin-bottom:10px; font-size:12px; color:var(--muted); }
  .incl label { display:flex; align-items:center; gap:5px; cursor:pointer; }
  .incl input { accent-color:var(--accent); cursor:pointer; }
  .preset { display:flex; gap:6px; flex-wrap:wrap; margin-bottom:8px; }
  .preset button { font-size:11px; padding:5px 9px; border-radius:7px; border:1px solid var(--line);
                   background:var(--panel2); color:var(--muted); cursor:pointer; }
  .preset button:hover{ color:var(--txt); border-color:var(--accent); }
  #mAction { width:100%; min-height:64px; resize:vertical; background:var(--panel2); color:var(--txt);
             border:1px solid var(--line); border-radius:8px; padding:9px 11px; font-size:13px;
             font-family:inherit; outline:none; }
  #mAction:focus{ border-color:var(--accent); }
  #sendBtn { margin-top:8px; width:100%; padding:10px; border-radius:8px; border:none;
             background:var(--accent); color:#fff; font-size:13px; font-weight:600; cursor:pointer; }
  #sendBtn:hover{ filter:brightness(1.1); } #sendBtn:disabled{ opacity:.5; cursor:default; }
</style>
</head>
<body>
<header>
  <h1>📸 Screenshots <span id="total"></span></h1>
  <input id="search" placeholder="Filter by text, summary, filename…" autocomplete="off">
  <div class="chips" id="catChips"></div>
  <div class="chips" id="flagChips"></div>
  <div id="count"></div>
</header>
<main id="grid"></main>

<div id="overlay">
  <div class="close" onclick="closeModal()">×</div>
  <div class="modal">
    <div class="img-pane"><img id="mImg" src=""></div>
    <div class="info">
      <div><h2 id="mFile"></h2><div class="small" id="mDate"></div></div>
      <div><div class="label">Summary</div><div id="mSum"></div></div>
      <div>
        <div class="label">Category <span class="saved" id="savedCat">saved ✓</span></div>
        <select id="mCat" onchange="saveCat()"></select>
      </div>
      <div>
        <div class="label">Flag <span class="saved" id="savedFlag">saved ✓</span></div>
        <div class="flagbtns" id="mFlags"></div>
      </div>
      <div id="actionBox">
        <div class="label">⚡ Send to <span id="botLabel"></span> <span class="saved" id="savedAction">on it ✓</span></div>
        <div class="incl" id="incl">
          <label title="Filename, category, source, date — cheap"><input type="checkbox" id="incMeta" checked> metadata</label>
          <label title="Full transcribed text — cheap, usually enough"><input type="checkbox" id="incOcr" checked> OCR text</label>
          <label title="One-line summary — cheap"><input type="checkbox" id="incSummary" checked> summary</label>
          <label title="Sends the actual image — costs vision tokens, only if the bot must SEE it"><input type="checkbox" id="incImage"> 🖼 image (costly)</label>
        </div>
        <div class="preset" id="presets"></div>
        <textarea id="mAction" placeholder="What should the bot do with this? (e.g. add this event to my calendar and find ticket prices)"></textarea>
        <button id="sendBtn" onclick="sendAction()">Send →</button>
        <div class="small" id="actionNote"></div>
      </div>
      <div><div class="label">Full OCR text</div><div class="ocr" id="mOcr"></div></div>
    </div>
  </div>
</div>

<script>
let DATA = [], cur = null;
const state = { q:"", cats:new Set(), flags:new Set() };

async function load() {
  DATA = await (await fetch('/api/screenshots')).json();
  document.getElementById('total').textContent = '· ' + DATA.length;
  buildChips();
  render();
}

function buildChips() {
  const cats = [...new Set(DATA.map(d=>d.category).filter(Boolean))].sort();
  const cc = document.getElementById('catChips');
  cats.forEach(c => {
    const el = document.createElement('div');
    el.className='chip'; el.textContent=c;
    el.onclick=()=>{ state.cats.has(c)?state.cats.delete(c):state.cats.add(c); el.classList.toggle('on'); render(); };
    cc.appendChild(el);
  });
  const fc = document.getElementById('flagChips');
  ['keep','review','delete'].forEach(f => {
    const el = document.createElement('div');
    el.className='chip flag-'+f; el.textContent=f;
    el.onclick=()=>{ state.flags.has(f)?state.flags.delete(f):state.flags.add(f); el.classList.toggle('on'); render(); };
    fc.appendChild(el);
  });
}

function match(d) {
  if (state.cats.size && !state.cats.has(d.category)) return false;
  if (state.flags.size && !state.flags.has(d.flag)) return false;
  if (state.q) {
    const hay = ((d.ocr_text||'')+' '+(d.summary||'')+' '+(d.filename||'')+' '+(d.category||'')).toLowerCase();
    if (!hay.includes(state.q)) return false;
  }
  return true;
}

function render() {
  const g = document.getElementById('grid');
  const items = DATA.filter(match);
  document.getElementById('count').textContent = items.length + ' shown';
  g.innerHTML = '';
  items.forEach(d => {
    const card = document.createElement('div');
    card.className='card'; card.onclick=()=>openModal(d);
    const img = d.exists
      ? `<img loading="lazy" src="/img/${d.uuid}">`
      : `<div class="missing">image unavailable<br>${d.filename||''}</div>`;
    card.innerHTML = img + `<div class="meta">
        <p class="sum">${esc(d.summary||d.filename||'—')}</p>
        <div class="tags"><span class="dot ${d.flag}"></span>
          <span class="tag">${d.category||'—'}</span></div></div>`;
    g.appendChild(card);
  });
}

function openModal(d) {
  cur = d;
  document.getElementById('mImg').src = d.exists ? '/img/'+d.uuid : '';
  document.getElementById('mFile').textContent = d.filename || d.uuid;
  document.getElementById('mDate').textContent =
     (d.source||'') + (d.date_taken? ' · '+d.date_taken : '');
  document.getElementById('mSum').textContent = d.summary || '—';
  document.getElementById('mOcr').textContent = d.ocr_text || '(no text)';
  const sel = document.getElementById('mCat'); sel.innerHTML='';
  CATS.forEach(c => { const o=document.createElement('option'); o.value=c; o.textContent=c;
     if(c===d.category)o.selected=true; sel.appendChild(o); });
  if (d.category && !CATS.includes(d.category)) {
     const o=document.createElement('option'); o.value=d.category; o.textContent=d.category; o.selected=true; sel.appendChild(o);
  }
  const fb = document.getElementById('mFlags'); fb.innerHTML='';
  ['keep','review','delete'].forEach(f => {
    const b=document.createElement('button'); b.textContent=f; b.className=f+(f===d.flag?' on':'');
    b.onclick=()=>saveFlag(f); fb.appendChild(b);
  });
  setupAction(d);
  document.getElementById('overlay').classList.add('on');
}
function closeModal(){ document.getElementById('overlay').classList.remove('on'); }

async function saveCat() {
  const v = document.getElementById('mCat').value;
  await update(cur.uuid, {category:v}); cur.category=v;
  flash('savedCat'); render();
}
async function saveFlag(f) {
  await update(cur.uuid, {flag:f}); cur.flag=f;
  document.querySelectorAll('#mFlags button').forEach(b=>b.classList.toggle('on', b.textContent===f));
  flash('savedFlag'); render();
}
async function update(uuid, fields) {
  await fetch('/api/update', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({uuid, ...fields})});
}
function flash(id){ const e=document.getElementById(id); e.classList.add('show'); setTimeout(()=>e.classList.remove('show'),1200); }
function esc(s){ return (s||'').replace(/[&<>]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

document.getElementById('search').addEventListener('input', e=>{ state.q=e.target.value.toLowerCase().trim(); render(); });
document.addEventListener('keydown', e=>{ if(e.key==='Escape') closeModal(); });
document.getElementById('overlay').addEventListener('click', e=>{ if(e.target.id==='overlay') closeModal(); });

const CATS = __CATS__;
const ACTION_ENABLED = __ACTION_ENABLED__;
const BOT_NAME = __BOT_NAME__;
const PRESETS = [
  "Add this to my calendar",
  "Check my calendar for conflicts on the date here",
  "Find prices / how much this costs",
  "Draft a reply / follow-up email about this",
  "Summarize and save the key info",
];

function setupAction(d) {
  const box = document.getElementById('actionBox');
  if (!ACTION_ENABLED) { box.style.display='none'; return; }
  box.style.display='block';
  document.getElementById('botLabel').textContent = BOT_NAME;
  document.getElementById('sendBtn').textContent = 'Send to '+BOT_NAME+' →';
  document.getElementById('mAction').value = '';
  document.getElementById('actionNote').textContent = '';
  const p = document.getElementById('presets'); p.innerHTML='';
  PRESETS.forEach(t => { const b=document.createElement('button'); b.textContent=t;
    b.onclick=()=>{ const a=document.getElementById('mAction'); a.value=(a.value?a.value+' ':'')+t; a.focus(); };
    p.appendChild(b); });
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
  const r = await (await fetch('/api/action', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({uuid:cur.uuid, instruction, include})})).json();
  btn.disabled=false; btn.textContent='Send to '+BOT_NAME+' →';
  if (r.ok) { flash('savedAction');
    document.getElementById('actionNote').textContent=BOT_NAME+' is on it — watch your chat for the reply.';
  } else { document.getElementById('actionNote').textContent='⚠ '+(r.error||r.msg||'failed'); }
}

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
                    .replace("__ACTION_ENABLED__", "true" if ACTION_ENABLED else "false")
                    .replace("__BOT_NAME__", json.dumps(BOT_NAME)))
            return self._send(200, html, "text/html; charset=utf-8")
        if u.path == "/api/screenshots":
            return self._send(200, json.dumps(fetch_all()))
        if u.path.startswith("/img/"):
            return self._serve_image(u.path[len("/img/"):], parse_qs(u.query))
        return self._send(404, json.dumps({"error": "not found"}))

    def _serve_image(self, uuid, qs):
        conn = db()
        row = conn.execute("SELECT path FROM screenshots WHERE uuid=?", (uuid,)).fetchone()
        conn.close()
        if not row or not row["path"] or not os.path.exists(row["path"]):
            return self._send(404, b"", "image/png")
        path = row["path"]
        # Thumbnail via Pillow when available (keeps the grid snappy on big libraries).
        if HAVE_PIL:
            try:
                im = Image.open(path)
                im.thumbnail((900, 900))
                buf = io.BytesIO()
                im.convert("RGB").save(buf, "JPEG", quality=82)
                data = buf.getvalue()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "max-age=86400")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                return self.wfile.write(data)
            except Exception:
                pass
        with open(path, "rb") as f:
            data = f.read()
        ctype = "image/png" if path.lower().endswith(".png") else "image/jpeg"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "max-age=86400")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path not in ("/api/update", "/api/action"):
            return self._send(404, json.dumps({"error": "not found"}))
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or "{}")
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
            ok, msg = dispatch_action(uuid, instruction, body.get("include"))
            return self._send(200, json.dumps({"ok": ok, "msg": msg}))

        sets, vals = [], []
        for col in ("category", "flag", "summary"):
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
