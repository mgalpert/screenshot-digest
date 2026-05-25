---
name: search-screenshots
description: >-
  Semantically search a screenshot-digest library (OCR text + AI summaries,
  ranked by meaning via hybrid keyword+vector search) and return the matching
  image. Use when the user asks to find, look up, or pull up a screenshot they
  remember — e.g. "find my screenshot about X", "what was that chart of Y",
  "send me the image referencing Z". Describes intent, not exact words.
---

# Search screenshots

This skill searches a local [`screenshot-digest`](https://github.com/mgalpert/screenshot-digest)
library by **meaning** and returns the best-matching screenshot image.

The library's viewer exposes a localhost HTTP API. Search is **hybrid**: it
fuses keyword matches with vector-embedding similarity (Reciprocal Rank Fusion),
so a query like *"from AI to AGI"* finds the right infographic even if those
exact words aren't in it.

## Prerequisites (one-time)

1. The viewer is running: `python3 screenshot_viewer.py` → `http://127.0.0.1:8765`
   (override host/port with `--host/--port`; set `VIEWER_URL` below to match).
2. Embeddings exist: `python3 screenshot_embed.py` has been run and
   `OPENAI_API_KEY` is set in the viewer's environment. Without them, search
   still works but falls back to keyword-only (no semantic ranking).

## How to search

```bash
VIEWER_URL=${VIEWER_URL:-http://127.0.0.1:8765}

# 1) Search. Returns {"uuids":[...], "rows":[...]} ranked best-first.
#    rows include uuid, summary, category, date_taken — enough to pick/caption.
curl -s "$VIEWER_URL/api/search?q=$(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))' "from AI to AGI")"
```

Take the **first** uuid (highest relevance) — or scan a few `rows` summaries to
choose the best fit for the user's intent.

## How to fetch the image to send back

uuids may contain spaces, colons, or U+202F, so always URL-encode and pass via
`?id=`. `&full=1` returns the original (omit it for a smaller thumbnail).

```bash
UUID="<uuid from search>"
ENC=$(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))' "$UUID")
curl -s "$VIEWER_URL/img?id=$ENC&full=1" -o /tmp/match.jpg
```

Then deliver `/tmp/match.jpg` to the user through whatever channel you have
(e.g. attach it to a chat/email). If your environment restricts file reads to a
boundary, save it inside that boundary first.

## Notes

- **Relevance order matters** — the API already ranks results; don't re-sort.
- **Caption with the summary** — `rows[0].summary` is a one-line description of
  the shot; it makes a good caption and confirms you picked the right one.
- **Other endpoints**: `GET /api/ocr?id=<uuid>` for the full OCR text of a shot;
  `GET /api/screenshots?status=&from=&to=` for windowed browsing. See the repo
  README's "For AI agents / automation" section for the full API.
