#!/usr/bin/env python3
"""
Mine stock/crypto price observations out of the screenshot library.

The OCR text already contains the tickers and prices (Robinhood watchlists,
portfolios, candlestick charts, news). This pass reads that text (no images,
so it's fast + cheap) and asks Gemini to pull STRUCTURED observations:
  {ticker, price, change_pct}  +  the screenshot's capture date as the timestamp.

Candidates are gathered by semantic search (hybrid index) ∪ keyword backstop,
then the LLM doubles as a filter — a shot with no real ticker/price yields
nothing and is recorded as processed so we don't re-ask.

Output: a `stock_observations` table in the same screenshots.db, feeding
stock_report.py. Resumable (skips uuids already processed).

Usage:
  stock_extract.py --limit 30     # validate on a sample
  stock_extract.py                # full candidate set
  stock_extract.py --status
"""

import argparse, json, os, sqlite3, sys, threading, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshot_digest as sd
import screenshot_viewer as v   # reuse the hybrid search to gather candidates

LOG = lambda m: print(f"{time.strftime('%H:%M:%S')} {m}", file=sys.stderr, flush=True)
REQUEST_TIMEOUT = 30

SEM_QUERIES = ["stock ticker price chart", "stock market portfolio", "robinhood watchlist",
               "candlestick trading chart", "investment account shares", "crypto price chart"]
KW_TERMS = ["Robinhood", "Watchlist", "NASDAQ", "candlestick"]


def ensure_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS stock_observations(
        uuid TEXT, obs_date TEXT, ticker TEXT, price REAL, change_pct REAL,
        source_type TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS stock_processed(uuid TEXT PRIMARY KEY)""")
    conn.commit()


def gather_candidates(conn):
    uu = set()
    for q in SEM_QUERIES:
        uu |= set(v.search_ranked(q)[:200])
    for term in KW_TERMS:
        uu |= {r[0] for r in conn.execute(
            "SELECT uuid FROM screenshots WHERE ocr_text LIKE ?", (f"%{term}%",)).fetchall()}
    return uu


def extract_one(uuid, summary, ocr, date):
    """Text-only Gemini call → list of observation dicts (possibly empty)."""
    instruction = (
        "This is OCR text from a screenshot taken on " + (date or "unknown date") + ". "
        "If it shows stock or crypto prices (a chart, watchlist, portfolio, or quote), "
        "extract each distinct instrument you can see with a current/last price. "
        "ticker = the symbol (e.g. AAPL, BTC, TSLA) uppercased; price = the per-share "
        "/ per-unit MARKET price as a number (no $ or commas); change_pct = the daily "
        "percent change if shown (signed number), else null. "
        "source_type one of: chart, watchlist, portfolio, quote, news, other. "
        "CRITICAL: never report a share COUNT, quantity, position size, or total "
        "holding value as price — those are not prices. On a portfolio/positions "
        "screen, include a ticker ONLY if an explicit per-share price is visible "
        "(e.g. '$142.50'); if only shares held and total value are shown, OMIT it. "
        "If it is NOT a stock/crypto price screenshot, return is_stock=false and an "
        "empty observations list. Only include instruments with a real per-unit price."
    )
    text = ((summary or "") + "\n\n" + (ocr or ""))[:7000]
    body = json.dumps({
        "contents": [{"parts": [{"text": instruction + "\n\nOCR:\n" + text}]}],
        "generationConfig": {
            "temperature": 0, "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingBudget": 0},
            "responseSchema": {
                "type": "object",
                "properties": {
                    "is_stock": {"type": "boolean"},
                    "source_type": {"type": "string",
                        "enum": ["chart", "watchlist", "portfolio", "quote", "news", "other"]},
                    "observations": {"type": "array", "items": {
                        "type": "object",
                        "properties": {
                            "ticker": {"type": "string"},
                            "price": {"type": "number"},
                            "change_pct": {"type": "number", "nullable": True},
                        }, "required": ["ticker", "price"]}},
                },
                "required": ["is_stock", "observations"],
            },
        },
    }).encode()
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{sd.GEMINI_MODEL}:generateContent?key={sd.GEMINI_KEY}")
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                data = json.loads(resp.read())
            d = json.loads(data["candidates"][0]["content"]["parts"][0]["text"])
            if not d.get("is_stock"):
                return []
            st = d.get("source_type", "other")
            out = []
            for o in d.get("observations", []):
                try:
                    tk = str(o["ticker"]).upper().strip()[:12]
                    pr = float(o["price"])
                    if not tk or pr <= 0:
                        continue
                    cp = o.get("change_pct")
                    out.append((uuid, date, tk, pr, float(cp) if cp is not None else None, st))
                except Exception:
                    continue
            return out
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
            time.sleep(2 ** attempt)
        except Exception:
            return []
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(sd.DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_table(conn)

    if args.status:
        nobs = conn.execute("SELECT count(*) FROM stock_observations").fetchone()[0]
        nt = conn.execute("SELECT count(DISTINCT ticker) FROM stock_observations").fetchone()[0]
        np = conn.execute("SELECT count(*) FROM stock_processed").fetchone()[0]
        print(f"processed {np} shots · {nobs} observations · {nt} distinct tickers")
        return

    done = {r[0] for r in conn.execute("SELECT uuid FROM stock_processed").fetchall()}
    LOG("gathering candidates (semantic ∪ keyword)…")
    cand = gather_candidates(conn)
    todo = [u for u in cand if u not in done]
    if args.limit:
        todo = todo[:args.limit]
    meta = {}
    for u in todo:
        r = conn.execute("SELECT summary, ocr_text, COALESCE(date_taken,date_added) d FROM screenshots WHERE uuid=?", (u,)).fetchone()
        if r:
            meta[u] = (r["summary"], r["ocr_text"], (r["d"] or "")[:10])
    LOG(f"{len(cand)} candidates, {len(todo)} to process, workers={args.workers}")

    t0 = time.monotonic(); n = nobs = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(extract_one, u, *meta[u]): u for u in todo if u in meta}
        for fut in as_completed(futs):
            u = futs[fut]; n += 1
            try:
                rows = fut.result()
            except Exception:
                rows = []
            if rows:
                conn.executemany(
                    "INSERT INTO stock_observations(uuid,obs_date,ticker,price,change_pct,source_type) VALUES (?,?,?,?,?,?)", rows)
                nobs += len(rows)
            conn.execute("INSERT OR IGNORE INTO stock_processed(uuid) VALUES (?)", (u,))
            if n % 50 == 0:
                conn.commit()
                rate = n / max(time.monotonic()-t0, 1) * 60
                LOG(f"{n}/{len(todo)} ({rate:.0f}/min) · {nobs} observations so far")
    conn.commit()
    LOG(f"DONE {n} shots → {nobs} observations in {(time.monotonic()-t0)/60:.1f} min")
    conn.close()


if __name__ == "__main__":
    main()
