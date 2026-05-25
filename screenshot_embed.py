#!/usr/bin/env python3
"""
Embed every screenshot's OCR text into a sqlite-vec table so the viewer can do
semantic + hybrid search across the whole library (~48k shots).

Pipeline mirror of the OCR backfill, but for vectors:
  • Reads rows that have OCR text but no embedding yet (resumable — skips done).
  • Builds an embedding input = summary + OCR text (summary is high-signal, OCR
    is the long tail), truncated to keep token cost bounded.
  • Batches to OpenAI text-embedding-3-small (1536d) — cheap (~$0.02/1M tokens,
    so the full library is well under a dollar) and fast.
  • Stores vectors in a vec0 virtual table in the SAME screenshots.db, keyed by
    the screenshot uuid (TEXT PK), so the existing backup picks it up for free.

Usage:
  screenshot_embed.py                 # embed everything missing
  screenshot_embed.py --limit 200     # smoke test
  screenshot_embed.py --status        # how many embedded vs pending, then exit

Env:
  OPENAI_API_KEY (required), EMBED_MODEL (default text-embedding-3-small)
"""

import argparse, os, sqlite3, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshot_digest as sd
import sqlite_vec
from openai import OpenAI

EMBED_MODEL = os.environ.get("EMBED_MODEL", "text-embedding-3-small")
EMBED_DIM = 1536
MAX_CHARS = 8000      # ~2k tokens/shot ceiling — keeps cost + latency bounded
BATCH = 100           # OpenAI allows large batches; 100 is a safe, fast chunk
LOG_PATH = Path(sd.DB_PATH).parent / "embed.log"


def log(msg: str):
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')}  {msg}"
    print(line, file=sys.stderr, flush=True)
    try:
        with LOG_PATH.open("a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def connect():
    conn = sqlite3.connect(sd.DB_PATH)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_shots "
        f"USING vec0(uuid TEXT PRIMARY KEY, embedding float[{EMBED_DIM}])")
    return conn


def embed_input(summary: str, ocr: str) -> str:
    summary = (summary or "").strip()
    ocr = (ocr or "").strip()
    text = (summary + "\n\n" + ocr).strip() if summary else ocr
    return text[:MAX_CHARS]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    conn = connect()
    embedded = {r[0] for r in conn.execute("SELECT uuid FROM vec_shots").fetchall()}
    total_with_ocr = conn.execute(
        "SELECT count(*) FROM screenshots WHERE COALESCE(ocr_text,'')!=''").fetchone()[0]

    if args.status:
        print(f"embedded: {len(embedded)} / {total_with_ocr} with OCR "
              f"({total_with_ocr - len(embedded)} pending)")
        return

    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("OPENAI_API_KEY not set")
    client = OpenAI()

    rows = conn.execute(
        "SELECT uuid, summary, ocr_text FROM screenshots "
        "WHERE COALESCE(ocr_text,'')!='' ORDER BY COALESCE(date_taken,date_added) DESC"
    ).fetchall()
    todo = [r for r in rows if r[0] not in embedded]
    if args.limit:
        todo = todo[:args.limit]

    total = len(todo)
    log(f"[embed] {total_with_ocr} with OCR, {len(embedded)} already embedded, "
        f"{total} to embed · model={EMBED_MODEL} dim={EMBED_DIM}")
    if not total:
        log("[embed] nothing to do.")
        return

    t0 = time.monotonic()
    done = 0
    for i in range(0, total, BATCH):
        chunk = todo[i:i + BATCH]
        inputs = [embed_input(s, o) for (_u, s, o) in chunk]
        # retry the batch on transient errors; embeddings are idempotent
        for attempt in range(5):
            try:
                resp = client.embeddings.create(model=EMBED_MODEL, input=inputs)
                break
            except Exception as e:
                wait = min(30, 2 ** attempt)
                log(f"[embed] batch error ({type(e).__name__}): {e} — retry in {wait}s")
                time.sleep(wait)
        else:
            log(f"[embed] giving up on batch at offset {i}; stopping so it stays resumable")
            break

        for (uuid, _s, _o), item in zip(chunk, resp.data):
            conn.execute("INSERT OR REPLACE INTO vec_shots(uuid, embedding) VALUES (?, ?)",
                         (uuid, sqlite_vec.serialize_float32(item.embedding)))
        conn.commit()
        done += len(chunk)
        rate = done / max(time.monotonic() - t0, 1) * 60
        eta = (total - done) / max(rate, 1)
        log(f"[embed] {done}/{total}  ({rate:.0f}/min, ETA {eta:.1f} min)")

    conn.close()
    log(f"[embed] DONE  embedded {done} in {(time.monotonic()-t0)/60:.1f} min")


if __name__ == "__main__":
    main()
