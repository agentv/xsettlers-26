#!/usr/bin/env python
"""
Renders tourney/summary.json + tourney/data/*.json (written by
scripts/run_tournament.py) into a single self-contained HTML report:
a leaderboard, an avg-score bar chart, one overlay chart of all
strategies' turn-by-turn total holdings, and one small-multiple line
chart per round-robin matchup. Pure static SVG (coordinates computed
here, not in client JS) plus native <title> hover tooltips on each
point -- no chart library, no network dependency, safe to open offline
or publish as an Artifact.

Color use follows the dataviz skill's documented default palette
(slots 1-5 of its 8-hue adjacent-pairlist order, used as a validated
prefix -- see docs/TODO.md or the skill itself for the six-check
rationale). The 10 per-matchup panels use only slots 1-2 (blue/orange)
as a *role* color (alphabetically-first strategy vs second), not a
fixed per-strategy hue -- 5 strategies exceeds the 3-series cap for
all-pairs-safe categorical color in a small-multiples form, so identity
there rides on the direct end-of-line labels, not hue. The single
overlay chart, by contrast, is one ordinary multi-line chart (the
"adjacent" pairlist applies, not all-pairs), so it uses a fixed hue per
strategy (slots 1-5 in a stable alphabetical order) plus a legend.

Usage:
    .venv/bin/python scripts/build_tournament_report.py
"""
import json
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOURNEY_DIR = f"{REPO_ROOT}/tourney"

SLOT = {  # (light, dark) -- dataviz skill's documented default palette, slots 1-5
    1: ("#2a78d6", "#3987e5"),  # blue
    2: ("#eb6834", "#d95926"),  # orange
    3: ("#1baf7a", "#199e70"),  # aqua
    4: ("#eda100", "#c98500"),  # yellow
    5: ("#e87ba4", "#d55181"),  # magenta
}

def _label(strategy: str) -> str:
    return strategy.replace("_", " ").title().replace("Npc", "NPC")

def _load():
    summary = json.load(open(f"{TOURNEY_DIR}/summary.json"))
    games = {}
    for m in summary["matchups"]:
        games[m["matchup"]] = json.load(open(f"{TOURNEY_DIR}/data/{m['matchup']}.json"))
    return summary, games

def _leaderboard(summary, games):
    records = {s: {"wins": 0, "losses": 0, "total_score": 0.0, "games": 0}
               for s in summary["strategies"]}
    for m in summary["matchups"]:
        standings = sorted(m["standings"], key=lambda s: -s["score"])
        winner, loser = standings[0], standings[1]
        records[winner["strategy"]]["wins"] += 1
        records[winner["strategy"]]["total_score"] += winner["score"]
        records[winner["strategy"]]["games"] += 1
        records[loser["strategy"]]["losses"] += 1
        records[loser["strategy"]]["total_score"] += loser["score"]
        records[loser["strategy"]]["games"] += 1
    rows = [(s, r["wins"], r["losses"], r["total_score"] / r["games"])
            for s, r in records.items()]
    rows.sort(key=lambda r: (-r[1], -r[3]))
    return rows

def _series_for_player(game, player_id):
    pts = [r for r in game["turn_data"] if r["player_id"] == player_id]
    pts.sort(key=lambda r: r["turn"])
    return pts

# ---- shared chart geometry -------------------------------------------------
Y_DOMAIN = (1000, 2350)
Y_TICKS = [1000, 1300, 1600, 1900, 2200]
X_DOMAIN = (0, 19)

def _sx(turn, x0, x1):
    t0, t1 = X_DOMAIN
    return x0 + (turn - t0) / (t1 - t0) * (x1 - x0)

def _sy(value, y0, y1):
    v0, v1 = Y_DOMAIN
    return y1 - (value - v0) / (v1 - v0) * (y1 - y0)

def _line_chart(width, height, margins, series, title, chart_id, aria_title=None) -> str:
    """series: list of (label, css_class, points[{turn,total}], end_label)."""
    ml, mr, mt, mb = margins
    x0, x1 = ml, width - mr
    y0, y1 = mt, height - mb
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" '
             f'aria-labelledby="{chart_id}-t">']
    parts.append(f'<title id="{chart_id}-t">{aria_title or title}</title>')

    for ytick in Y_TICKS:
        y = _sy(ytick, y0, y1)
        parts.append(f'<line class="grid" x1="{x0}" y1="{y:.1f}" x2="{x1}" y2="{y:.1f}"/>')
        parts.append(f'<text class="tick-label" x="{x0 - 6}" y="{y+3:.1f}" '
                     f'text-anchor="end">{ytick}</text>')
    for xtick in (0, 5, 10, 15, 19):
        x = _sx(xtick, x0, x1)
        parts.append(f'<text class="tick-label" x="{x:.1f}" y="{y1+14}" '
                     f'text-anchor="middle">{xtick}</text>')
    parts.append(f'<line class="baseline" x1="{x0}" y1="{y1}" x2="{x1}" y2="{y1}"/>')
    if title:
        parts.append(f'<text class="chart-title" x="{ml}" y="14">{title}</text>')

    end_labels = []
    for label, css_class, points, _end in series:
        coords = [(_sx(p["turn"], x0, x1), _sy(p["total"], y0, y1), p) for p in points]
        d = " ".join(f'{"M" if i==0 else "L"}{x:.1f},{y:.1f}' for i, (x, y, _) in enumerate(coords))
        parts.append(f'<path class="{css_class}" d="{d}" fill="none"/>')
        for x, y, p in coords:
            parts.append(f'<circle class="{css_class}-pt" cx="{x:.1f}" cy="{y:.1f}" r="2.6">'
                         f'<title>{label}, turn {p["turn"]}: {p["total"]:.0f}</title></circle>')
        end_labels.append((coords[-1][1], css_class, label))

    end_labels.sort(key=lambda t: t[0])
    for i in range(1, len(end_labels)):
        prev_y = end_labels[i-1][0]
        y, cls, label = end_labels[i]
        if y - prev_y < 13:
            end_labels[i] = (prev_y + 13, cls, label)
    for y, css_class, label in end_labels:
        y = min(max(y, y0 + 6), y1 - 2)
        parts.append(f'<text class="{css_class}-label" x="{x1+5}" y="{y+3:.1f}">{label}</text>')

    parts.append("</svg>")
    return "\n".join(parts)

def _bar_chart(rows, width=720, height=230) -> str:
    ml, mr, mt, mb = 150, 60, 16, 24
    x0, x1 = ml, width - mr
    y0, y1 = mt, height - mb
    max_score = max(r[3] for r in rows) * 1.08
    bar_h = (y1 - y0) / len(rows) * 0.6
    gap = (y1 - y0) / len(rows)
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" '
             f'aria-labelledby="bar-t"><title id="bar-t">Average score by strategy</title>']
    for i, (strategy, wins, losses, avg) in enumerate(rows):
        y = y0 + i * gap + (gap - bar_h) / 2
        w = (avg / max_score) * (x1 - x0)
        parts.append(f'<text class="tick-label" x="{ml-8}" y="{y+bar_h/2+4:.1f}" '
                     f'text-anchor="end">{_label(strategy)}</text>')
        parts.append(f'<rect class="bar" x="{x0}" y="{y:.1f}" width="{w:.1f}" height="{bar_h:.1f}" rx="3">'
                     f'<title>{_label(strategy)}: {avg:.0f} avg pts, {wins}-{losses}</title></rect>')
        parts.append(f'<text class="bar-value" x="{x0+w+6:.1f}" y="{y+bar_h/2+4:.1f}">{avg:.0f}</text>')
    parts.append(f'<line class="baseline" x1="{x0}" y1="{y0}" x2="{x0}" y2="{y1}"/>')
    parts.append("</svg>")
    return "\n".join(parts)

def build():
    summary, games = _load()
    rows = _leaderboard(summary, games)
    order = [s for s, *_ in rows]  # strategy display order, best first
    alpha_order = sorted(summary["strategies"])
    fixed_slot = {s: i + 1 for i, s in enumerate(alpha_order)}  # 1..5, for overview chart

    css_vars = []
    for slot, (light, dark) in SLOT.items():
        css_vars.append(f"  --series-{slot}: {light};")
    css_vars_dark = []
    for slot, (light, dark) in SLOT.items():
        css_vars_dark.append(f"    --series-{slot}: {dark};")

    overview_series = []
    for s in alpha_order:
        # any game featuring this strategy has its full trajectory; take the first
        m = next(m for m in summary["matchups"] if s in (m["strategy_1"], m["strategy_2"]))
        game = games[m["matchup"]]
        pid = next(p["id"] for p in game["players"] if p["strategy"] == s)
        pts = _series_for_player(game, pid)
        overview_series.append((_label(s), f"s{fixed_slot[s]}", pts, _label(s)))
    overview_svg = _line_chart(760, 360, (56, 130, 20, 34), overview_series, "", "overview",
                               aria_title="Resource level over time for all 5 strategies")

    panel_svgs = []
    for m in summary["matchups"]:
        game = games[m["matchup"]]
        s1, s2 = sorted((m["strategy_1"], m["strategy_2"]))  # alphabetical -> role order
        p1 = next(p["id"] for p in game["players"] if p["strategy"] == s1)
        p2 = next(p["id"] for p in game["players"] if p["strategy"] == s2)
        series = [
            (_label(s1), "s1", _series_for_player(game, p1), _label(s1)),
            (_label(s2), "s2", _series_for_player(game, p2), _label(s2)),
        ]
        title = f"{_label(s1)} vs {_label(s2)}"
        panel_svgs.append(_line_chart(360, 210, (46, 96, 26, 30), series, title, m["matchup"]))

    bar_svg = _bar_chart(rows)

    leaderboard_rows = "\n".join(
        f'<tr><td>{i+1}</td><td>{_label(s)}</td><td class="num">{w}-{l}</td>'
        f'<td class="num">{avg:.0f}</td></tr>'
        for i, (s, w, l, avg) in enumerate(rows)
    )
    legend_items = "\n".join(
        f'<span class="legend-item"><span class="swatch s{fixed_slot[s]}"></span>{_label(s)}</span>'
        for s in alpha_order
    )
    panels_html = "\n".join(f'<div class="panel">{svg}</div>' for svg in panel_svgs)

    html = f"""<title>NPC Strategy Round Robin — Diaspora</title>
<style>
  .viz-root {{
    color-scheme: light;
{chr(10).join(css_vars)}
    --surface-1: #fcfcfb; --page: #f9f9f7; --text-primary: #0b0b0b;
    --text-secondary: #52514e; --muted: #898781; --grid: #e1e0d9; --baseline: #c3c2b7;
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    background: var(--page); color: var(--text-primary);
    padding: 24px; max-width: 1180px; margin: 0 auto;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:where(:not([data-theme="light"])) .viz-root {{
      color-scheme: dark;
{chr(10).join(css_vars_dark)}
      --surface-1: #1a1a19; --page: #0d0d0d; --text-primary: #ffffff;
      --text-secondary: #c3c2b7; --muted: #898781; --grid: #2c2c2a; --baseline: #383835;
    }}
  }}
  :root[data-theme="dark"] .viz-root {{
    color-scheme: dark;
{chr(10).join(css_vars_dark)}
    --surface-1: #1a1a19; --page: #0d0d0d; --text-primary: #ffffff;
    --text-secondary: #c3c2b7; --muted: #898781; --grid: #2c2c2a; --baseline: #383835;
  }}
  h1 {{ font-size: 22px; margin-bottom: 4px; }}
  h2 {{ font-size: 16px; margin: 32px 0 8px; }}
  p.sub {{ color: var(--text-secondary); margin-top: 0; }}
  p.caption {{ color: var(--muted); font-size: 13px; }}
  section {{ background: var(--surface-1); border-radius: 10px; padding: 18px 20px; margin-bottom: 20px; }}
  table {{ border-collapse: collapse; width: 100%; max-width: 480px; }}
  th, td {{ text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--grid); }}
  td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .legend-row {{ display: flex; flex-wrap: wrap; gap: 14px; margin-bottom: 8px; }}
  .legend-item {{ display: flex; align-items: center; gap: 6px; font-size: 13px; color: var(--text-secondary); }}
  .swatch {{ width: 10px; height: 10px; border-radius: 2px; display: inline-block; }}
  .swatch.s1, .s1, .s1-pt, .s1-label {{ color: var(--series-1); }}
  .swatch.s2, .s2, .s2-pt, .s2-label {{ color: var(--series-2); }}
  .swatch.s3, .s3, .s3-pt, .s3-label {{ color: var(--series-3); }}
  .swatch.s4, .s4, .s4-pt, .s4-label {{ color: var(--series-4); }}
  .swatch.s5, .s5, .s5-pt, .s5-label {{ color: var(--series-5); }}
  .swatch.s1 {{ background: var(--series-1); }}
  .swatch.s2 {{ background: var(--series-2); }}
  .swatch.s3 {{ background: var(--series-3); }}
  .swatch.s4 {{ background: var(--series-4); }}
  .swatch.s5 {{ background: var(--series-5); }}
  path.s1, path.s2, path.s3, path.s4, path.s5 {{ stroke-width: 2; stroke: currentColor; }}
  circle.s1-pt, circle.s2-pt, circle.s3-pt, circle.s4-pt, circle.s5-pt {{ fill: currentColor; }}
  text.s1-label, text.s2-label, text.s3-label, text.s4-label, text.s5-label {{
    fill: currentColor; font-size: 11px; dominant-baseline: middle;
  }}
  .chart {{ width: 100%; height: auto; }}
  .chart-title {{ font-size: 12px; fill: var(--text-secondary); }}
  .grid {{ stroke: var(--grid); stroke-width: 1; }}
  .baseline {{ stroke: var(--baseline); stroke-width: 1; }}
  .tick-label {{ font-size: 10px; fill: var(--muted); }}
  .bar {{ fill: var(--series-1); }}
  .bar-value {{ font-size: 11px; fill: var(--text-secondary); dominant-baseline: middle; }}
  .panels-grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 14px; }}
  .panel {{ background: var(--page); border: 1px solid var(--grid); border-radius: 8px; padding: 6px; }}
  @media (max-width: 760px) {{ .panels-grid {{ grid-template-columns: 1fr; }} }}
</style>
<div class="viz-root">
  <h1>NPC Strategy Round Robin — Diaspora (game0)</h1>
  <p class="sub">5 registered strategies, every pair played once, 20 turns each, no combat.
  Resource level = total stored energy + food + goods across a player's whole fleet.</p>

  <section>
    <h2>Leaderboard</h2>
    <table>
      <thead><tr><th>Rank</th><th>Strategy</th><th class="num">W-L</th><th class="num">Avg score</th></tr></thead>
      <tbody>
{leaderboard_rows}
      </tbody>
    </table>
    <p class="caption">Score = 2×goods + 1×food + 0×energy (config/game_config.yaml's score_weights),
    applied to each player's final stockpile.</p>
  </section>

  <section>
    <h2>Average score by strategy</h2>
    {bar_svg}
  </section>

  <section>
    <h2>Resource level over time — all 5 strategies</h2>
    <div class="legend-row">
{legend_items}
    </div>
    {overview_svg}
    <p class="caption">Each strategy's own trajectory, independent of who it happened to play —
    see "what this tournament actually measured" below.</p>
  </section>

  <section>
    <h2>Every matchup, turn by turn</h2>
    <p class="caption">Color here marks role (alphabetically-first strategy = blue, second = orange),
    not a fixed strategy identity — with 5 strategies and 10 pairings, no color assignment stays
    both fixed and reliably distinguishable across every pair, so identity rides on the end-of-line
    labels instead. Full per-turn figures for every game are also saved as plain JSON in
    tourney/data/, not only charted here.</p>
    <div class="panels-grid">
{panels_html}
    </div>
  </section>
</div>
"""
    out_path = f"{TOURNEY_DIR}/report.html"
    with open(out_path, "w") as f:
        f.write(html)
    print(f"Wrote {out_path}")

if __name__ == "__main__":
    build()
