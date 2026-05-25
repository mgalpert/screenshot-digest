#!/usr/bin/env python3
"""
Render the mined stock observations (stock_extract.py) into a self-contained
HTML price-timeline report: one row per ticker with an inline-SVG price-over-
time chart built purely from your screenshots, sorted by how often each ticker
shows up (most-screenshotted = most interesting to you).

  stock_report.py                 # -> data/stock_timeline.html
  stock_report.py --min-obs 2     # only tickers seen at least N times
"""

import argparse, html, sqlite3, sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshot_digest as sd

OUT = Path(sd.DB_PATH).parent / "stock_timeline.html"


def spark(points, w=320, h=48):
    """Inline SVG line chart from [(date, price)] (min-max scaled per ticker)."""
    if len(points) < 2:
        return '<span class="single">single data point</span>'
    prices = [p for _, p in points]
    lo, hi = min(prices), max(prices)
    rng = (hi - lo) or 1.0
    n = len(points)
    pts = []
    for i, (_d, p) in enumerate(points):
        x = (i / (n - 1)) * (w - 4) + 2
        y = h - 2 - ((p - lo) / rng) * (h - 4)
        pts.append(f"{x:.1f},{y:.1f}")
    up = points[-1][1] >= points[0][1]
    color = "#16a34a" if up else "#dc2626"
    dots = "".join(f'<circle cx="{px}" cy="{py}" r="1.5" fill="{color}"/>'
                   for px, py in (p.split(",") for p in pts))
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
            f'<polyline fill="none" stroke="{color}" stroke-width="1.5" points="{" ".join(pts)}"/>'
            f'{dots}</svg>')


def fmt(p):
    if p >= 1000:
        return f"{p:,.0f}"
    if p >= 1:
        return f"{p:,.2f}"
    return f"{p:.6f}".rstrip("0").rstrip(".")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-obs", type=int, default=2)
    args = ap.parse_args()

    conn = sqlite3.connect(sd.DB_PATH)
    rows = conn.execute(
        "SELECT ticker, obs_date, price FROM stock_observations "
        "WHERE obs_date!='' AND price>0 ORDER BY ticker, obs_date").fetchall()
    total_obs = len(rows)
    span = conn.execute("SELECT min(obs_date), max(obs_date) FROM stock_observations WHERE obs_date!=''").fetchone()
    conn.close()

    series = defaultdict(list)
    for tk, d, p in rows:
        series[tk].append((d, p))

    # sort tickers by observation count desc, then alpha
    ordered = sorted(series.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    shown = [(tk, pts) for tk, pts in ordered if len(pts) >= args.min_obs]

    cards = []
    for tk, pts in shown:
        first_d, first_p = pts[0]
        last_d, last_p = pts[-1]
        chg = (last_p - first_p) / first_p * 100 if first_p else 0
        chg_cls = "up" if chg >= 0 else "down"
        cards.append(f"""
        <tr>
          <td class="tk">{html.escape(tk)}</td>
          <td class="n">{len(pts)}</td>
          <td class="span">{first_d[:7]} → {last_d[:7]}</td>
          <td class="price">${fmt(first_p)} → ${fmt(last_p)}</td>
          <td class="chg {chg_cls}">{chg:+.0f}%</td>
          <td class="chart">{spark(pts)}</td>
        </tr>""")

    n_single = sum(1 for tk, pts in ordered if len(pts) < args.min_obs)
    doc = f"""<!doctype html><meta charset="utf-8">
<title>Stock Timeline — from your screenshots</title>
<style>
  body {{ font:14px -apple-system,system-ui,sans-serif; margin:0; background:#0b0d12; color:#e6e8ee; }}
  header {{ padding:28px 32px; border-bottom:1px solid #1e222b; }}
  h1 {{ margin:0 0 6px; font-size:22px; }}
  .sub {{ color:#8b93a7; font-size:13px; }}
  table {{ border-collapse:collapse; width:100%; }}
  td,th {{ padding:10px 16px; border-bottom:1px solid #161a22; text-align:left; }}
  th {{ position:sticky; top:0; background:#11141b; color:#8b93a7; font-weight:600; font-size:12px; text-transform:uppercase; letter-spacing:.04em; }}
  .tk {{ font-weight:700; font-size:15px; }}
  .n,.chg {{ text-align:right; font-variant-numeric:tabular-nums; }}
  .price,.span {{ color:#aab2c5; font-variant-numeric:tabular-nums; white-space:nowrap; }}
  .up {{ color:#16a34a; }} .down {{ color:#dc2626; }}
  .chart {{ width:340px; }}
  tr:hover {{ background:#10131a; }}
</style>
<header>
  <h1>📈 Stock Timeline</h1>
  <div class="sub">{total_obs:,} price observations mined from your screenshots ·
  {len(series):,} distinct tickers · {span[0][:10] if span[0] else '?'} → {span[1][:10] if span[1] else '?'} ·
  showing {len(shown):,} tickers seen ≥{args.min_obs}× ({n_single:,} one-off tickers hidden)</div>
</header>
<table>
  <thead><tr><th>Ticker</th><th class="n">Shots</th><th>Span</th><th>First → Last seen</th><th class="chg">Δ</th><th>Price over time</th></tr></thead>
  <tbody>{''.join(cards)}</tbody>
</table>
"""
    OUT.write_text(doc)
    print(f"wrote {OUT}  ({len(shown)} tickers charted, {total_obs} observations)")


if __name__ == "__main__":
    main()
