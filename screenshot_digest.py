#!/usr/bin/env python3
"""
Screenshot OCR + categorization pipeline (macOS).

OCRs your screenshots (Photos library + Desktop), classifies each one, flags it
keep / review / delete, and produces a daily markdown digest. Default engine is
Gemini Flash (multimodal, OCR + classification in one call); falls back to local
Apple Vision OCR + rule-based classification with --local or when no API key.

Usage:
  screenshot_digest.py              # process new Photos screenshots from last 24h
  screenshot_digest.py --days N     # look back N days (default 1)
  screenshot_digest.py --all        # reprocess everything
  screenshot_digest.py --desktop    # include ~/Desktop PNG screenshots
  screenshot_digest.py --report     # write markdown report and print path
  screenshot_digest.py --local      # force offline (Apple Vision + rules), no API
  screenshot_digest.py --show TEXT  # show full OCR text for a filename substring

Setup: see README.md. Requires Python 3.10+, osxphotos, ocrmac,
and a Gemini API key in GEMINI_OCR_KEY (or GEMINI_API_KEY).
"""

import argparse, json, os, re, sqlite3, sys, textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Storage location — override with SCREENSHOT_DIGEST_HOME (default ~/.screenshot-digest)
_HOME = Path(os.environ.get("SCREENSHOT_DIGEST_HOME", str(Path.home() / ".screenshot-digest")))
DB_PATH = _HOME / "screenshots.db"
REPORTS_DIR = _HOME / "reports"

CATEGORIES = [
    "contact_info",
    "url_link",
    "code_snippet",
    "receipt_financial",
    "social_media",
    "conversation",
    "calendar_event",
    "map_location",
    "article_text",
    "product_ui",
    "document",
    "photo_media",
    "misc",
]

FLAG_KEEP   = "keep"
FLAG_DELETE = "delete"
FLAG_REVIEW = "review"

CAT_LABELS = {
    "contact_info":     "📇 Contact Info",
    "url_link":         "🔗 URLs & Links",
    "code_snippet":     "💻 Code / Terminal",
    "receipt_financial":"💰 Receipts & Finance",
    "social_media":     "📱 Social Media",
    "conversation":     "💬 Conversations",
    "calendar_event":   "📅 Calendar / Events",
    "map_location":     "🗺️ Maps & Locations",
    "article_text":     "📰 Articles & Reading",
    "product_ui":       "🖥️ App UI / Mockups",
    "document":         "📄 Documents",
    "photo_media":      "🖼️ Photos & Media",
    "misc":             "📦 Misc",
}

FLAG_EMOJI = {FLAG_KEEP: "✅", FLAG_DELETE: "🗑️", FLAG_REVIEW: "👁️"}


# ── DB ──────────────────────────────────────────────────────────────────────

def init_db(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS screenshots (
            uuid          TEXT PRIMARY KEY,
            path          TEXT NOT NULL,
            filename      TEXT,
            date_taken    TEXT,
            date_added    TEXT,
            date_processed TEXT,
            ocr_text      TEXT,
            category      TEXT,
            flag          TEXT,
            summary       TEXT,
            source        TEXT DEFAULT 'photos',
            -- Authoritative triage workflow state (owned by the viewer). `flag`
            -- above is only the AI's advisory keep/review/delete recommendation.
            status        TEXT DEFAULT 'needs_review'
        );
    """)
    # Migrate older DBs that predate later columns.
    existing = [c[1] for c in conn.execute("PRAGMA table_info(screenshots)").fetchall()]
    if "date_added" not in existing:
        conn.execute("ALTER TABLE screenshots ADD COLUMN date_added TEXT")
    if "status" not in existing:
        conn.execute("ALTER TABLE screenshots ADD COLUMN status TEXT DEFAULT 'needs_review'")
        conn.execute("UPDATE screenshots SET status='needs_review' WHERE status IS NULL OR status=''")
    conn.commit()


# ── SOURCE DISCOVERY ────────────────────────────────────────────────────────

def get_photos_screenshots(days_back: int) -> list[dict]:
    """Query Photos library for screenshots via osxphotos."""
    try:
        import osxphotos
    except ImportError:
        print("[warn] osxphotos not available", file=sys.stderr)
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    try:
        lib = osxphotos.PhotosDB()
    except Exception as e:
        print(f"[warn] Cannot open Photos library: {e}", file=sys.stderr)
        return []
    results = []
    for photo in lib.photos(images=True):
        if not photo.screenshot:
            continue
        if photo.date and photo.date.replace(tzinfo=timezone.utc) < cutoff:
            continue
        # Originals are usually in iCloud (path=None). Fall back to the largest
        # locally-cached derivative (preview JPEG), which is fine for OCR.
        path = photo.path
        if not path or not Path(path).exists():
            derivs = [d for d in (photo.path_derivatives or []) if Path(d).exists()]
            if not derivs:
                continue
            path = max(derivs, key=lambda d: Path(d).stat().st_size)
        results.append({
            "uuid": photo.uuid,
            "path": str(path),
            "filename": photo.original_filename or Path(path).name,
            "date_taken": photo.date.isoformat() if photo.date else None,
            "date_added": photo.date_added.isoformat() if getattr(photo, "date_added", None) else None,
            "source": "photos",
        })
    return results


def get_desktop_screenshots(days_back: int) -> list[dict]:
    """Find screenshots on the Desktop newer than days_back."""
    cutoff = datetime.now() - timedelta(days=days_back)
    results = []
    for p in sorted((Path.home() / "Desktop").glob("Screenshot*.png")):
        st = p.stat()
        # st_birthtime = true creation/save time (unchanged by edits); fall back to mtime
        created = getattr(st, "st_birthtime", st.st_mtime)
        if datetime.fromtimestamp(created) < cutoff:
            continue
        results.append({
            "uuid": f"desktop:{p.name}",
            "path": str(p),
            "filename": p.name,
            "date_taken": datetime.fromtimestamp(created).isoformat(),
            "date_added": datetime.fromtimestamp(st.st_mtime).isoformat(),
            "source": "desktop",
        })
    return results


# ── OCR ─────────────────────────────────────────────────────────────────────

def ocr_image(path: str) -> str:
    """OCR an image locally using Apple Vision (ocrmac)."""
    try:
        from ocrmac.ocrmac import OCR
        items = OCR(path, language_preference=["en-US"]).recognize()
        if items:
            return "\n".join(item[0] for item in items if item[0].strip())
        return ""
    except Exception as e:
        print(f"[warn] OCR failed for {Path(path).name}: {e}", file=sys.stderr)
        return ""


# ── CATEGORIZATION ──────────────────────────────────────────────────────────

_EMAIL   = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_PHONE   = re.compile(r"(\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}")
_URL     = re.compile(r"https?://\S+|www\.\S+|\S+\.com/\S*")
_MONEY   = re.compile(r"[\$€£¥]\s*[\d,]+\.?\d*|\d+[\.,]\d{2}\s*(USD|EUR|GBP)")
_DATE    = re.compile(r"\b(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}|\w+ \d{1,2},?\s*\d{4})\b")
_TIME    = re.compile(r"\b\d{1,2}:\d{2}\s*(AM|PM|am|pm)?\b")

_RULES: list[tuple[str, str, list[str]]] = [
    # (category, flag, [keywords/patterns])
    ("code_snippet",    FLAG_KEEP,   ["def ", "function ", "import ", "export ", "npm ", "git ",
                                      "sudo ", "$ ", "console.", "TypeError", "SyntaxError",
                                      "traceback", "stack trace", "#!/", ".py", ".js", ".ts"]),
    ("receipt_financial", FLAG_KEEP, ["receipt", "invoice", "total", "subtotal", "order #",
                                      "transaction", "charged", "refund", "billing", "payment"]),
    ("calendar_event",  FLAG_KEEP,   ["calendar", "invite", "meeting", "zoom", "google meet",
                                      "teams", "webinar", "rsvp", "agenda", "schedule"]),
    ("document",        FLAG_KEEP,   ["agreement", "contract", "pursuant", "signature", "policy",
                                      "terms of service", "privacy", "hereby", "whereas"]),
    ("social_media",    FLAG_REVIEW, ["retweet", "likes", "followers", "following",
                                      "instagram", "tiktok", "x.com", "twitter.com",
                                      "linkedin.com", "facebook.com", "posted •"]),
    ("conversation",    FLAG_REVIEW, ["delivered", "read receipt", "typing...", "imessage",
                                      "whatsapp", "telegram", "signal", "slack"]),
    ("map_location",    FLAG_DELETE, ["directions", "miles away", "minutes away",
                                      "get directions", "current location", "maps.apple",
                                      "maps.google"]),
    ("article_text",    FLAG_KEEP,   ["published", "min read", "subscribe", "newsletter",
                                      "by the editors", "read more", "full article"]),
    ("product_ui",      FLAG_DELETE, ["settings", "preferences", "notifications", "app store",
                                      "update available", "allow", "deny", "permission"]),
]


def rule_categorize(ocr_text: str, filename: str) -> dict:
    if not ocr_text.strip():
        return {"category": "misc", "flag": FLAG_DELETE,
                "summary": "No text detected — likely a photo or blank screen"}

    t = ocr_text.lower()

    # high-confidence pattern checks first
    if _EMAIL.search(ocr_text) or _PHONE.search(ocr_text):
        return {"category": "contact_info", "flag": FLAG_KEEP,
                "summary": _first_line(ocr_text)}

    if _MONEY.search(ocr_text) and any(w in t for w in ["receipt", "total", "invoice", "order", "charged"]):
        return {"category": "receipt_financial", "flag": FLAG_KEEP,
                "summary": _first_line(ocr_text)}

    if _URL.search(ocr_text) and len(ocr_text) < 300:
        return {"category": "url_link", "flag": FLAG_KEEP,
                "summary": _first_line(ocr_text)}

    # keyword rules
    for category, flag, keywords in _RULES:
        if any(k.lower() in t for k in keywords):
            return {"category": category, "flag": flag,
                    "summary": _first_line(ocr_text)}

    # length heuristic: short = UI noise, long = article worth reading
    word_count = len(ocr_text.split())
    if word_count > 100:
        return {"category": "article_text", "flag": FLAG_REVIEW,
                "summary": _first_line(ocr_text)}
    if word_count < 10:
        return {"category": "misc", "flag": FLAG_DELETE,
                "summary": _first_line(ocr_text) or "Minimal text content"}

    return {"category": "misc", "flag": FLAG_REVIEW,
            "summary": _first_line(ocr_text)}


def _first_line(text: str, max_chars: int = 120) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:max_chars]
    return text[:max_chars]


GEMINI_MODEL = os.environ.get("SCREENSHOT_GEMINI_MODEL", "gemini-3.5-flash")


def _resolve_gemini_key() -> str:
    """Gemini API key from GEMINI_OCR_KEY, then GEMINI_API_KEY. Also reads a local
    .env file (cwd or SCREENSHOT_DIGEST_HOME) so you don't have to export it."""
    for var in ("GEMINI_OCR_KEY", "GEMINI_API_KEY"):
        if os.environ.get(var):
            return os.environ[var]
    for envfile in (Path.cwd() / ".env", _HOME / ".env"):
        try:
            for line in envfile.read_text().splitlines():
                line = line.strip().removeprefix("export ").strip()
                for var in ("GEMINI_OCR_KEY=", "GEMINI_API_KEY="):
                    if line.startswith(var):
                        return line.split("=", 1)[1].strip().strip('"\'')
        except Exception:
            pass
    return ""


GEMINI_KEY = _resolve_gemini_key()


def gemini_analyze(image_path: str) -> dict | None:
    """ONE multimodal Gemini call: image -> {ocr_text, category, flag, summary}.
    Most reliable path (esp. complex screenshots). Returns None on error."""
    if not GEMINI_KEY:
        return None
    import urllib.request, base64
    cats = ", ".join(CATEGORIES)
    try:
        img_b64 = base64.b64encode(Path(image_path).read_bytes()).decode()
    except Exception as e:
        print(f"[warn] cannot read image {image_path}: {e}", file=sys.stderr)
        return None

    instruction = (
        "You are processing a screenshot for someone managing their screenshot "
        "library. Do two things:\n"
        "1) ocr_text: transcribe ALL text visible in the image, exactly, in reading order.\n"
        f"2) classify it: category must be exactly one of [{cats}]; "
        "flag is keep (genuinely useful info), delete (transient/junk/duplicate/blank), "
        "or review (unclear); summary is one concise sentence describing the screenshot."
    )
    body = json.dumps({
        "contents": [{"parts": [
            {"text": instruction},
            {"inline_data": {"mime_type": "image/jpeg", "data": img_b64}},
        ]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingBudget": 0},
            "responseSchema": {
                "type": "object",
                "properties": {
                    "ocr_text": {"type": "string"},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "flag": {"type": "string", "enum": [FLAG_KEEP, FLAG_DELETE, FLAG_REVIEW]},
                    "summary": {"type": "string"},
                },
                "required": ["ocr_text", "category", "flag", "summary"],
            },
        },
    }).encode()
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}")
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        d = json.loads(data["candidates"][0]["content"]["parts"][0]["text"])
        cat = d.get("category") if d.get("category") in CATEGORIES else "misc"
        flag = d.get("flag") if d.get("flag") in (FLAG_KEEP, FLAG_DELETE, FLAG_REVIEW) else FLAG_REVIEW
        return {
            "ocr_text": d.get("ocr_text", "") or "",
            "category": cat,
            "flag": flag,
            "summary": (d.get("summary") or "")[:240],
        }
    except Exception as e:
        print(f"[warn] gemini_analyze failed ({GEMINI_MODEL}): {e}", file=sys.stderr)
        return None


def llm_categorize(ocr_text: str, filename: str) -> dict:
    """Categorize via Gemini Flash. Falls back to rules on any error."""
    if not ocr_text.strip():
        return rule_categorize(ocr_text, filename)
    if not GEMINI_KEY:
        print("[warn] no Gemini API key (GEMINI_API_KEY / OPENCLAW_GOOGLE_NANO_API_KEY)", file=sys.stderr)
        return rule_categorize(ocr_text, filename)

    import urllib.request
    cats = ", ".join(CATEGORIES)
    prompt = textwrap.dedent(f"""
        Classify this screenshot's OCR text for someone managing their screenshot library.
        Filename: {filename}
        OCR text (first 1500 chars):
        ---
        {ocr_text[:1500]}
        ---
        Rules:
        - "category" MUST be exactly one of these slugs (no other values): {cats}
        - "flag": keep = genuinely useful info worth saving (contact, receipt, code, link/article to act on);
                  delete = transient/junk (notification, ad, duplicate, transient UI, blank);
                  review = unclear, let the user decide.
        - "summary": one concise sentence describing what the screenshot is about.
    """).strip()

    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            # Classification is trivial — disable "thinking" to cut output-token
            # cost ~5x (thinking tokens bill at the output rate).
            "thinkingConfig": {"thinkingBudget": 0},
            "responseSchema": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": CATEGORIES},
                    "flag": {"type": "string", "enum": [FLAG_KEEP, FLAG_DELETE, FLAG_REVIEW]},
                    "summary": {"type": "string"},
                },
                "required": ["category", "flag", "summary"],
            },
        },
    }).encode()

    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}")
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        d = json.loads(text)
        cat = d.get("category") if d.get("category") in CATEGORIES else "misc"
        flag = d.get("flag") if d.get("flag") in (FLAG_KEEP, FLAG_DELETE, FLAG_REVIEW) else FLAG_REVIEW
        return {"category": cat, "flag": flag, "summary": (d.get("summary") or "")[:240]}
    except Exception as e:
        print(f"[warn] Gemini categorize failed ({GEMINI_MODEL}): {e}", file=sys.stderr)
        return rule_categorize(ocr_text, filename)


# ── PROCESSING ──────────────────────────────────────────────────────────────

def process_screenshot(conn, item: dict, force: bool = False, local_only: bool = False) -> dict | None:
    uuid = item["uuid"]
    if not force:
        if conn.execute("SELECT 1 FROM screenshots WHERE uuid=?", (uuid,)).fetchone():
            return None

    path = item["path"]
    if not Path(path).exists():
        return None

    print(f"  → {item['filename']}", file=sys.stderr)

    # Default: one multimodal Gemini call does OCR + classification together
    # (most reliable, esp. on complex screenshots). Fall back to local Apple
    # Vision OCR + rule categorization if --local or Gemini is unavailable/errors.
    result = None if local_only else gemini_analyze(path)
    if result:
        ocr_text = result["ocr_text"]
        cat_data = {"category": result["category"], "flag": result["flag"], "summary": result["summary"]}
    else:
        ocr_text = ocr_image(path)
        cat_data = rule_categorize(ocr_text, item["filename"])

    # INSERT OR REPLACE deletes the old row, so any user triage (status) would be
    # lost on a --all reprocess. Carry the existing status forward; new shots start
    # at needs_review.
    prev = conn.execute("SELECT status FROM screenshots WHERE uuid=?", (uuid,)).fetchone()
    status = (prev[0] if prev and prev[0] else "needs_review")

    row = {
        "uuid": uuid,
        "path": path,
        "filename": item["filename"],
        "date_taken": item.get("date_taken"),
        "date_added": item.get("date_added"),
        "date_processed": datetime.now().isoformat(),
        "ocr_text": ocr_text,
        "category": cat_data["category"],
        "flag": cat_data["flag"],
        "summary": cat_data["summary"],
        "source": item.get("source", "photos"),
        "status": status,
    }
    conn.execute("""
        INSERT OR REPLACE INTO screenshots
        (uuid,path,filename,date_taken,date_added,date_processed,ocr_text,category,flag,summary,source,status)
        VALUES
        (:uuid,:path,:filename,:date_taken,:date_added,:date_processed,:ocr_text,:category,:flag,:summary,:source,:status)
    """, row)
    conn.commit()
    return row


# ── DIGEST ──────────────────────────────────────────────────────────────────

def build_digest(conn, days_back: int) -> str:
    cutoff = (datetime.now() - timedelta(days=days_back)).isoformat()
    rows = conn.execute("""
        SELECT * FROM screenshots
        WHERE date_taken >= ? OR date_processed >= ?
        ORDER BY date_taken DESC
    """, (cutoff, cutoff)).fetchall()

    if not rows:
        return "No screenshots in this period."

    cols = [d[1] for d in conn.execute("PRAGMA table_info(screenshots)").fetchall()]
    def r(row): return dict(zip(cols, row))

    by_flag = {FLAG_KEEP: [], FLAG_DELETE: [], FLAG_REVIEW: []}
    for row in rows:
        d = r(row)
        by_flag.get(d["flag"], by_flag[FLAG_REVIEW]).append(d)

    total = len(rows)
    n_keep = len(by_flag[FLAG_KEEP])
    n_del  = len(by_flag[FLAG_DELETE])
    n_rev  = len(by_flag[FLAG_REVIEW])

    # Triage progress (the viewer's workflow axis) — distinct from the AI's
    # keep/review/delete recommendation that we group by below.
    n_needs = sum(1 for row in rows if (r(row).get("status") or "needs_review") == "needs_review")
    n_done  = total - n_needs

    lines = [
        f"# Screenshot Digest — {datetime.now().strftime('%Y-%m-%d')}",
        "",
        f"> **{total}** screenshots · ✅ {n_keep} keep · 🗑️ {n_del} delete · 👁️ {n_rev} review",
        f"> Triage: 🟠 {n_needs} need review · ✔️ {n_done} handled  "
        f"_(grouping below is the AI's recommendation, not your triage state)_",
        "",
    ]

    STATUS_MARK = {"reviewed": " ✔️ reviewed", "archived": " 🗄️ archived"}

    for flag, heading in [(FLAG_KEEP, "✅ Worth Keeping"), (FLAG_REVIEW, "👁️ Review These"), (FLAG_DELETE, "🗑️ Safe to Delete")]:
        group = by_flag[flag]
        if not group:
            continue
        lines += [f"## {heading} ({len(group)})", ""]
        by_cat: dict[str, list] = {}
        for d in group:
            by_cat.setdefault(d["category"], []).append(d)
        for cat, items in sorted(by_cat.items()):
            lines.append(f"### {CAT_LABELS.get(cat, cat)}")
            for d in items:
                dt   = (d["date_taken"] or "")[:16].replace("T", " ")
                fname = d["filename"] or ""
                summ  = d["summary"] or ""
                path  = d["path"]
                mark  = STATUS_MARK.get(d.get("status") or "needs_review", "")
                lines += [
                    f"- **{fname}** ({dt}){mark}  ",
                    f"  {summ}  ",
                    f"  `{path}`",
                    "",
                ]
            lines.append("")

    return "\n".join(lines)


# ── MAIN ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days",    type=int, default=1, help="Days to look back")
    parser.add_argument("--all",     action="store_true", help="Reprocess all")
    parser.add_argument("--desktop", action="store_true", help="Include Desktop screenshots")
    parser.add_argument("--report",  action="store_true", help="Save markdown report")
    parser.add_argument("--local",   action="store_true", help="Force offline (Apple Vision OCR + rules), skip Gemini")
    parser.add_argument("--llm",      action="store_true", help="(deprecated, no-op — Gemini is now the default)")
    parser.add_argument("--show",    type=str, default="",  help="Print full OCR for filename matching SUBSTR")
    args = parser.parse_args()

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    # --show mode: dump stored OCR text
    if args.show:
        rows = conn.execute(
            "SELECT filename, ocr_text FROM screenshots WHERE filename LIKE ?",
            (f"%{args.show}%",)
        ).fetchall()
        for fname, text in rows:
            print(f"\n=== {fname} ===\n{text}\n")
        conn.close()
        return

    days_back = 9999 if args.all else args.days

    print(f"[screenshot_digest] scanning last {days_back} day(s)...", file=sys.stderr)
    screenshots = get_photos_screenshots(days_back)
    if args.desktop or args.all:
        screenshots += get_desktop_screenshots(days_back)
    print(f"[screenshot_digest] {len(screenshots)} screenshot(s) found", file=sys.stderr)

    engine = "local (Apple Vision + rules)" if args.local else (
        f"Gemini {GEMINI_MODEL}" if GEMINI_KEY else "local fallback (no Gemini key)")
    print(f"[screenshot_digest] engine: {engine}", file=sys.stderr)

    new_count = 0
    for item in screenshots:
        row = process_screenshot(conn, item, force=args.all, local_only=args.local)
        if row:
            new_count += 1

    print(f"[screenshot_digest] {new_count} new, rest already cached", file=sys.stderr)

    digest = build_digest(conn, days_back)

    if args.report:
        report_path = REPORTS_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.md"
        report_path.write_text(digest)
        print(f"Report: {report_path}")
    else:
        print(digest)

    conn.close()


if __name__ == "__main__":
    main()
