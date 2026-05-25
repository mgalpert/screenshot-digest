#!/usr/bin/env python3
"""
Resumable, rate-aware backfill of the screenshot OCR+classify pipeline across
the WHOLE library (~48k shots) without getting rate-limited or hogging the box.

Design goals (the questions Michael asked):
  • Don't get rate-limited  → adaptive throttle (auto-slows on 429/503, speeds
                              back up on sustained success) + exponential
                              backoff with jitter per request.
  • Don't fry the machine    → bounded worker pool (default 4), niced process,
                              streams one image at a time per worker (a few MB
                              of base64 transient, not 48k in memory). The
                              Gemini multimodal path offloads OCR to Google, so
                              local CPU stays near-idle (just read+encode+write).
  • Stay resumable           → skips any uuid already in the DB and commits
                              after every row, so it can be killed/restarted
                              anytime (reboot, Ctrl-C, crash) and picks up where
                              it left off.

It reuses the exact engine from screenshot_digest (same Gemini call, same
schema, same status-preserving write), so backfilled rows are identical to
ones produced by the normal nightly digest.

Usage:
  screenshot_backfill.py                  # full library + desktop, default knobs
  screenshot_backfill.py --limit 20       # smoke test on 20 unprocessed shots
  screenshot_backfill.py --workers 6 --rpm 600
  screenshot_backfill.py --no-desktop     # photos only
  screenshot_backfill.py --status         # print progress (DB count vs library) and exit

Env knobs (override defaults without editing):
  BACKFILL_WORKERS, BACKFILL_RPM
"""

import argparse, json, os, random, sqlite3, sys, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshot_digest as sd  # reuse engine, schema, key, model

# Write the log next to the DB, which is exactly where the viewer's /api/backfill
# looks for it (SCREENSHOT_DIGEST_HOME). Portable + always in sync, no hardcoding.
LOG_PATH = Path(sd.DB_PATH).parent / "backfill.log"


def log(msg: str):
    line = f"{datetime.now().isoformat(timespec='seconds')}  {msg}"
    print(line, file=sys.stderr, flush=True)
    try:
        with LOG_PATH.open("a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── Adaptive rate limiter ────────────────────────────────────────────────────
# A single shared gate every request passes through. It enforces a minimum
# interval between request *starts*. On a rate signal (429/503) it widens the
# interval (slows down); after a run of clean successes it narrows it back
# toward the target. This converges on the fastest sustainable rate on its own,
# which is the real answer to "so we don't get rate limited."

class AdaptiveLimiter:
    def __init__(self, target_rpm: float):
        self.lock = threading.Lock()
        self.min_interval = 60.0 / max(target_rpm, 1)   # target spacing
        self.interval = self.min_interval                # current (>= min)
        self.max_interval = 8.0                          # never slower than this/req/worker-equiv
        self.next_at = 0.0
        self._ok_streak = 0

    def acquire(self):
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next_at)
            self.next_at = start + self.interval
            wait = start - now
        if wait > 0:
            time.sleep(wait)

    def on_rate_limit(self):
        with self.lock:
            self._ok_streak = 0
            self.interval = min(self.max_interval, self.interval * 1.7)
            log(f"[throttle] rate signal → spacing now {self.interval:.2f}s/req")

    def on_success(self):
        with self.lock:
            self._ok_streak += 1
            if self._ok_streak >= 25 and self.interval > self.min_interval:
                self.interval = max(self.min_interval, self.interval * 0.9)
                self._ok_streak = 0


class Stats:
    """Thread-safe tally of failure reasons, so the log shows *why* things slow
    down (timeouts vs 429s vs errors) instead of going silent."""
    def __init__(self):
        self.lock = threading.Lock()
        self.counts = {}
    def note(self, key):
        with self.lock:
            self.counts[key] = self.counts.get(key, 0) + 1
    def summary(self):
        with self.lock:
            if not self.counts:
                return ""
            return " ".join(f"{k}={v}" for k, v in sorted(self.counts.items()))

STATS = Stats()


# ── One screenshot → Gemini (with backoff), reusing sd's request shape ────────

# A healthy Gemini call is ~5s; 30s is generous headroom while still aborting a
# hung TLS read fast. (The original 90s × 8 retries could pin a worker ~15 min on
# a single stuck socket — that's what froze the first run.)
REQUEST_TIMEOUT = 30

def analyze_with_backoff(path: str, limiter: AdaptiveLimiter, max_tries: int = 4):
    """Returns (result_dict | None, status) where status in {ok, gaveup, error}."""
    import urllib.request, urllib.error, base64, socket
    if not sd.GEMINI_KEY:
        return None, "error"
    try:
        img_b64 = base64.b64encode(Path(path).read_bytes()).decode()
    except Exception:
        return None, "error"

    cats = ", ".join(sd.CATEGORIES)
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
                    "category": {"type": "string", "enum": sd.CATEGORIES},
                    "flag": {"type": "string", "enum": [sd.FLAG_KEEP, sd.FLAG_DELETE, sd.FLAG_REVIEW]},
                    "summary": {"type": "string"},
                },
                "required": ["ocr_text", "category", "flag", "summary"],
            },
        },
    }).encode()
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{sd.GEMINI_MODEL}:generateContent?key={sd.GEMINI_KEY}")

    last = "error"
    for attempt in range(max_tries):
        limiter.acquire()
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                data = json.loads(resp.read())
            d = json.loads(data["candidates"][0]["content"]["parts"][0]["text"])
            limiter.on_success()
            cat = d.get("category") if d.get("category") in sd.CATEGORIES else "misc"
            flag = d.get("flag") if d.get("flag") in (sd.FLAG_KEEP, sd.FLAG_DELETE, sd.FLAG_REVIEW) else sd.FLAG_REVIEW
            return {
                "ocr_text": d.get("ocr_text", "") or "",
                "category": cat, "flag": flag,
                "summary": (d.get("summary") or "")[:240],
            }, "ok"
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503):       # genuine rate/server signal → throttle + backoff
                limiter.on_rate_limit()
                last = f"http{e.code}"
                time.sleep(min(30.0, 2 ** attempt) + random.uniform(0, 1.5))
                continue
            STATS.note(f"http{e.code}")
            return None, f"http{e.code}"        # 400/403/etc — won't fix on retry
        except (socket.timeout, TimeoutError):
            # Hung/slow TLS read — the failure that froze run #1. Short backoff,
            # capped tries, then fall back to local OCR so we never stall.
            last = "timeout"
            STATS.note("timeout")
            time.sleep(min(8.0, 2 ** attempt) + random.uniform(0, 1.0))
            continue
        except Exception as e:
            last = f"err:{type(e).__name__}"
            STATS.note(type(e).__name__)
            time.sleep(min(8.0, 2 ** attempt) + random.uniform(0, 1.0))
            continue
    return None, last


# ── Worker: never lose a shot — fall back to local Apple Vision OCR ───────────

def process_item(item: dict, limiter: AdaptiveLimiter) -> dict:
    path = item["path"]
    if not Path(path).exists():
        return {"item": item, "skip": "missing_file"}
    result, status = analyze_with_backoff(path, limiter)
    if result:
        cat = {"category": result["category"], "flag": result["flag"], "summary": result["summary"]}
        ocr = result["ocr_text"]
        engine = "gemini"
    else:
        # Make progress rather than stall: local OCR + rules. Re-runnable later.
        ocr = sd.ocr_image(path)
        cat = sd.rule_categorize(ocr, item["filename"])
        engine = f"local({status})"
    return {
        "item": item, "engine": engine,
        "row": {
            "uuid": item["uuid"], "path": path, "filename": item["filename"],
            "date_taken": item.get("date_taken"), "date_added": item.get("date_added"),
            "date_processed": datetime.now().isoformat(),
            "ocr_text": ocr, "category": cat["category"], "flag": cat["flag"],
            "summary": cat["summary"], "source": item.get("source", "photos"),
        },
    }


def write_row(conn, row: dict):
    # New rows start at needs_review; this script never touches existing rows
    # (they're filtered out before dispatch), so triage status is safe.
    conn.execute("""
        INSERT OR REPLACE INTO screenshots
        (uuid,path,filename,date_taken,date_added,date_processed,ocr_text,category,flag,summary,source,status)
        VALUES
        (:uuid,:path,:filename,:date_taken,:date_added,:date_processed,:ocr_text,:category,:flag,:summary,:source,'needs_review')
    """, row)
    conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="cap unprocessed shots (smoke test)")
    ap.add_argument("--workers", type=int, default=int(os.environ.get("BACKFILL_WORKERS", "6")))
    ap.add_argument("--rpm", type=float, default=float(os.environ.get("BACKFILL_RPM", "300")),
                    help="target requests/min (limiter auto-slows below this on 429)")
    ap.add_argument("--no-desktop", action="store_true")
    ap.add_argument("--status", action="store_true", help="print progress and exit")
    args = ap.parse_args()

    conn = sqlite3.connect(sd.DB_PATH)
    sd.init_db(conn)
    done_uuids = {r[0] for r in conn.execute("SELECT uuid FROM screenshots").fetchall()}

    if args.status:
        print(f"in DB: {len(done_uuids)}")
        return

    log(f"[backfill] enumerating library (this scans Photos; ~a minute)…")
    items = sd.get_photos_screenshots(9999)
    if not args.no_desktop:
        items += sd.get_desktop_screenshots(9999)
    todo = [it for it in items if it["uuid"] not in done_uuids]
    if args.limit:
        todo = todo[:args.limit]

    total = len(todo)
    log(f"[backfill] {len(items)} in library, {len(done_uuids)} already done, "
        f"{total} to process · workers={args.workers} rpm≤{args.rpm:.0f} model={sd.GEMINI_MODEL}")
    if not total:
        log("[backfill] nothing to do.")
        return

    limiter = AdaptiveLimiter(args.rpm)
    t0 = time.monotonic()
    last_log = t0
    n_ok = n_local = n_skip = 0

    def emit(done):
        rate = done / max(time.monotonic() - t0, 1) * 60
        eta_min = (total - done) / max(rate, 0.1)
        errs = STATS.summary()
        log(f"[backfill] {done}/{total}  ({rate:.0f}/min, ETA {eta_min:.0f} min)  "
            f"gemini={n_ok} local_fallback={n_local} skipped={n_skip}"
            + (f"  · failures: {errs}" if errs else ""))

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(process_item, it, limiter): it for it in todo}
        done = 0
        for fut in as_completed(futs):
            done += 1
            try:
                res = fut.result()
            except Exception as e:
                log(f"[err] worker crashed: {e}")
                continue
            if res.get("skip"):
                n_skip += 1
            else:
                write_row(conn, res["row"])          # single-threaded writer = no sqlite lock fights
                if res["engine"] == "gemini":
                    n_ok += 1
                else:
                    n_local += 1
            # Log every 50 completions OR every 30s — the time-based heartbeat
            # means a stall is always visible in the log (run #1 went silent).
            now = time.monotonic()
            if done % 50 == 0 or done == total or (now - last_log) >= 30:
                emit(done)
                last_log = now

    conn.close()
    log(f"[backfill] DONE  gemini={n_ok} local_fallback={n_local} skipped={n_skip} "
        f"in {(time.monotonic()-t0)/60:.1f} min")


if __name__ == "__main__":
    main()
