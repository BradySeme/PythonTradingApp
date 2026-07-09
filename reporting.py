"""
Reporting Module v2 — Trade & P/L Report Generator
====================================================
Generates an HTML session log every 6 hours. Each snapshot shows:

  - Realized P/L, win rate, profit factor, max drawdown
  - Best / worst trade, avg hold time (wins vs losses)
  - Filter pass/reject stats
  - OPEN positions with live unrealized P/L (new)
  - Cumulative P/L chart, equity curve with buy/sell markers, drawdown chart
  - Per-symbol P/L charts
  - Win rate by pattern and by symbol
  - Trade log (last 100 trades)

Improvements over v1:
  - Snapshots are collapsible; only the newest is expanded by default
  - Charts render lazily when a snapshot is expanded, so the page stays
    fast no matter how many snapshots accumulate
  - The chart-rendering code lives in the page ONCE instead of being
    duplicated into every snapshot (v1 bloated the file every 6 hours)
  - Old snapshots are pruned automatically (keeps the newest 40 = ~10 days)
  - Timestamps display in your local timezone
  - If an old-format report file exists it is preserved as
    report_history_old.html and a fresh file is started

Same integration as before — instructions at the bottom of this file.
"""

import json
import os
import re
from datetime import datetime, timezone
from collections import defaultdict


REPORT_FILE = "report_history.html"
REPORT_INTERVAL_HOURS = 1
MAX_SNAPSHOTS = 40          # prune oldest beyond this (~10 days at 6h)
MAX_TRADE_ROWS = 100        # cap trade-log rows per snapshot

SNAP_START = "<!--SNAP_START-->"
SNAP_END = "<!--SNAP_END-->"
INSERT_POINT = "<!--INSERT_POINT-->"


# ─────────────────────────────────────────────
#  DATA FETCHING
# ─────────────────────────────────────────────

def _fetch_all_orders(trading_client):
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus
    orders = trading_client.get_orders(filter=GetOrdersRequest(
        status=QueryOrderStatus.CLOSED,
        limit=500,
    ))
    filled = [o for o in orders if o.filled_at is not None]
    filled.sort(key=lambda o: o.filled_at)
    return filled


def _fetch_portfolio_history(trading_client):
    try:
        history = trading_client.get_portfolio_history(period="1W", timeframe="1H")
        timestamps = history.timestamp or []
        equity = history.equity or []
        return [
            {"x": datetime.fromtimestamp(t, tz=timezone.utc).isoformat(), "y": round(e, 2)}
            for t, e in zip(timestamps, equity) if e is not None
        ]
    except Exception:
        return []


def _fetch_open_positions(trading_client):
    try:
        positions = trading_client.get_all_positions()
        out = []
        for p in positions:
            out.append({
                "symbol": p.symbol,
                "qty": float(p.qty),
                "avg_entry": float(p.avg_entry_price),
                "current": float(p.current_price or 0),
                "market_value": float(p.market_value or 0),
                "unrealized_pl": float(p.unrealized_pl or 0),
                "unrealized_plpc": float(p.unrealized_plpc or 0) * 100,
            })
        return out
    except Exception:
        return []


# ─────────────────────────────────────────────
#  P/L CALCULATION
# ─────────────────────────────────────────────

def _calculate_pnl(orders, pattern_log):
    positions = defaultdict(lambda: {"qty": 0.0, "cost": 0.0, "entry_time": None})
    records = []

    for o in orders:
        symbol = o.symbol
        side = o.side.value if hasattr(o.side, "value") else str(o.side)
        qty = float(o.filled_qty)
        price = float(o.filled_avg_price)
        filled_at = o.filled_at
        pattern = pattern_log.get(str(o.id), "unknown")

        pos = positions[symbol]

        if side == "buy":
            pos["cost"] += qty * price
            pos["qty"] += qty
            pos["entry_time"] = filled_at
            records.append({
                "symbol": symbol, "side": "buy", "qty": qty, "price": price,
                "time": filled_at.isoformat(), "pnl": None,
                "hold_minutes": None, "pattern": pattern,
            })
        elif side == "sell":
            avg_cost = (pos["cost"] / pos["qty"]) if pos["qty"] > 0 else price
            realized_pnl = (price - avg_cost) * qty
            hold_minutes = None
            if pos["entry_time"]:
                hold_minutes = round((filled_at - pos["entry_time"]).total_seconds() / 60, 1)
            pos["qty"] -= qty
            pos["cost"] -= avg_cost * qty
            if pos["qty"] <= 0.0001:
                pos["qty"], pos["cost"], pos["entry_time"] = 0.0, 0.0, None

            records.append({
                "symbol": symbol, "side": "sell", "qty": qty, "price": price,
                "time": filled_at.isoformat(), "pnl": round(realized_pnl, 2),
                "hold_minutes": hold_minutes, "pattern": pattern,
            })

    return records


def _compute_stats(records, equity_curve, fstats):
    sells = [r for r in records if r["side"] == "sell" and r["pnl"] is not None]
    total_pnl = sum(r["pnl"] for r in sells)
    wins = [r for r in sells if r["pnl"] > 0]
    losses = [r for r in sells if r["pnl"] <= 0]
    win_rate = round(len(wins) / len(sells) * 100, 1) if sells else 0

    gross_win = sum(r["pnl"] for r in wins)
    gross_loss = abs(sum(r["pnl"] for r in losses))
    profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else (
        float("inf") if gross_win > 0 else 0)

    best = max(sells, key=lambda r: r["pnl"], default=None)
    worst = min(sells, key=lambda r: r["pnl"], default=None)

    pattern_stats = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
    symbol_stats = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
    for r in sells:
        for stats_dict, key in ((pattern_stats, r["pattern"]), (symbol_stats, r["symbol"])):
            s = stats_dict[key]
            s["wins" if r["pnl"] > 0 else "losses"] += 1
            s["pnl"] += r["pnl"]

    hold = [r["hold_minutes"] for r in sells if r["hold_minutes"] is not None]
    win_hold = [r["hold_minutes"] for r in wins if r["hold_minutes"] is not None]
    loss_hold = [r["hold_minutes"] for r in losses if r["hold_minutes"] is not None]

    max_drawdown, peak = 0.0, None
    for point in equity_curve:
        val = point["y"]
        if peak is None or val > peak:
            peak = val
        if peak and peak > 0:
            max_drawdown = max(max_drawdown, (peak - val) / peak * 100)

    total_signals = fstats.get("passed", 0) + fstats.get("rejected", 0)

    return {
        "total_pnl": total_pnl,
        "total_trades": len(records),
        "wins": len(wins), "losses": len(losses),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "best_trade": best, "worst_trade": worst,
        "avg_hold": round(sum(hold) / len(hold), 1) if hold else 0,
        "avg_win_hold": round(sum(win_hold) / len(win_hold), 1) if win_hold else 0,
        "avg_loss_hold": round(sum(loss_hold) / len(loss_hold), 1) if loss_hold else 0,
        "max_drawdown": round(max_drawdown, 2),
        "pattern_stats": dict(pattern_stats),
        "symbol_stats": dict(symbol_stats),
        "filter_passed": fstats.get("passed", 0),
        "filter_rejected": fstats.get("rejected", 0),
        "rejection_rate": round(fstats.get("rejected", 0) / total_signals * 100, 1) if total_signals else 0,
    }


# ─────────────────────────────────────────────
#  HTML BUILDING
# ─────────────────────────────────────────────

def _local_time(iso_string, fmt="%m-%d %H:%M"):
    """Convert a UTC ISO timestamp to the machine's local timezone."""
    try:
        return datetime.fromisoformat(iso_string).astimezone().strftime(fmt)
    except Exception:
        return iso_string[11:16]


def _rate_rows(stats_dict):
    rows = ""
    for key, s in sorted(stats_dict.items()):
        total = s["wins"] + s["losses"]
        wr = round(s["wins"] / total * 100, 1) if total else 0
        color = "#34D399" if wr >= 50 else "#F87171"
        cls = "pos" if s["pnl"] >= 0 else "neg"
        rows += (f'<tr><td>{key}</td><td>{total}</td>'
                 f'<td style="color:{color}">{wr}%</td>'
                 f'<td class="{cls}">${s["pnl"]:.2f}</td></tr>')
    return rows or '<tr><td colspan="4" class="empty">No closed trades yet</td></tr>'


def _positions_rows(open_positions):
    if not open_positions:
        return '<tr><td colspan="6" class="empty">No open positions</td></tr>'
    rows = ""
    for p in open_positions:
        cls = "pos" if p["unrealized_pl"] >= 0 else "neg"
        sign = "+" if p["unrealized_pl"] >= 0 else ""
        rows += (f'<tr><td>{p["symbol"]}</td><td>{p["qty"]:.4f}</td>'
                 f'<td>${p["avg_entry"]:.2f}</td><td>${p["current"]:.2f}</td>'
                 f'<td>${p["market_value"]:,.2f}</td>'
                 f'<td class="{cls}">{sign}${p["unrealized_pl"]:.2f} '
                 f'({sign}{p["unrealized_plpc"]:.2f}%)</td></tr>')
    return rows


def _build_snapshot_html(records, equity_curve, open_positions, stats, snapshot_dt, is_open):
    uid = snapshot_dt.strftime("%Y%m%d_%H%M")
    display_time = snapshot_dt.strftime("%Y-%m-%d %H:%M")

    by_symbol = defaultdict(list)
    for r in records:
        by_symbol[r["symbol"]].append(r)

    # Chart data
    running = 0
    combined_pnl = []
    for r in records:
        if r["pnl"] is not None:
            running += r["pnl"]
        combined_pnl.append({"x": r["time"], "y": round(running, 2)})

    sym_pnl = {}
    for sym, recs in by_symbol.items():
        run, pts = 0, []
        for r in recs:
            if r["pnl"] is not None:
                run += r["pnl"]
            pts.append({"x": r["time"], "y": round(run, 2)})
        sym_pnl[sym] = pts

    chart_data = {
        "combinedPnl": combined_pnl,
        "equity": equity_curve,
        "buys": [{"x": r["time"], "y": r["price"]} for r in records if r["side"] == "buy"],
        "sells": [{"x": r["time"], "y": r["price"]} for r in records if r["side"] == "sell"],
        "symPnl": sym_pnl,
    }

    pnl_color = "#34D399" if stats["total_pnl"] >= 0 else "#F87171"
    pnl_sign = "+" if stats["total_pnl"] >= 0 else ""
    dd = stats["max_drawdown"]
    dd_color = "#F87171" if dd > 5 else "#FBBF24" if dd > 2 else "#34D399"
    pf = stats["profit_factor"]
    pf_str = "∞" if pf == float("inf") else f"{pf}"
    pf_color = "#34D399" if (pf == float("inf") or pf >= 1) else "#F87171"

    best = stats["best_trade"]
    worst = stats["worst_trade"]
    best_str = f'{best["symbol"]} +${best["pnl"]:.2f}' if best else "—"
    worst_str = f'{worst["symbol"]} ${worst["pnl"]:.2f}' if worst else "—"

    trade_rows = ""
    shown = list(reversed(records))[:MAX_TRADE_ROWS]
    for r in shown:
        pnl_str = f'${r["pnl"]:.2f}' if r["pnl"] is not None else "—"
        cls = "pos" if (r["pnl"] or 0) >= 0 else "neg"
        hold_str = f'{r["hold_minutes"]}m' if r["hold_minutes"] is not None else "—"
        trade_rows += (
            f'<tr><td>{_local_time(r["time"])}</td><td>{r["symbol"]}</td>'
            f'<td class="{r["side"]}">{r["side"].upper()}</td>'
            f'<td>{r["qty"]:.4f}</td><td>${r["price"]:.2f}</td>'
            f'<td>{r["pattern"]}</td><td>{hold_str}</td>'
            f'<td class="{cls}">{pnl_str}</td></tr>'
        )
    truncated_note = (f'<p class="note">Showing latest {MAX_TRADE_ROWS} of '
                      f'{stats["total_trades"]} trades.</p>'
                      if stats["total_trades"] > MAX_TRADE_ROWS else "")

    symbol_chart_divs = "".join(
        f'<div class="chart-box"><h4>{sym}</h4>'
        f'<canvas id="sym_{sym.replace("/", "")}_{uid}" height="170"></canvas></div>'
        for sym in by_symbol
    )

    equity_section = ""
    if equity_curve:
        equity_section = (
            f'<div class="chart-box wide"><h4>Portfolio value · buy/sell markers</h4>'
            f'<canvas id="eq_{uid}" height="190"></canvas></div>'
            f'<div class="chart-box wide"><h4>Drawdown</h4>'
            f'<canvas id="dd_{uid}" height="140"></canvas></div>'
        )

    open_attr = " open" if is_open else ""

    return f"""{SNAP_START}
<details class="snapshot" id="snap_{uid}"{open_attr}>
  <summary>
    <span class="snap-time">{display_time}</span>
    <span class="pill">{stats["total_trades"]} trades</span>
    <span class="pill" style="color:{pnl_color}">{pnl_sign}${stats["total_pnl"]:,.2f}</span>
    <span class="pill">WR {stats["win_rate"]}%</span>
    <span class="pill" style="color:{dd_color}">DD {dd}%</span>
    <span class="caret">▾</span>
  </summary>
  <div class="snapshot-body">

    <div class="stat-grid">
      <div class="stat-card"><div class="stat-val" style="color:{pnl_color}">{pnl_sign}${stats["total_pnl"]:,.2f}</div><div class="stat-lbl">Realized P/L</div></div>
      <div class="stat-card"><div class="stat-val">{stats["win_rate"]}%</div><div class="stat-lbl">Win rate · {stats["wins"]}W {stats["losses"]}L</div></div>
      <div class="stat-card"><div class="stat-val" style="color:{pf_color}">{pf_str}</div><div class="stat-lbl">Profit factor</div></div>
      <div class="stat-card"><div class="stat-val" style="color:{dd_color}">{dd}%</div><div class="stat-lbl">Max drawdown</div></div>
      <div class="stat-card"><div class="stat-val">{stats["avg_hold"]}m</div><div class="stat-lbl">Avg hold · W {stats["avg_win_hold"]}m / L {stats["avg_loss_hold"]}m</div></div>
      <div class="stat-card"><div class="stat-val pos">{best_str}</div><div class="stat-lbl">Best trade</div></div>
      <div class="stat-card"><div class="stat-val neg">{worst_str}</div><div class="stat-lbl">Worst trade</div></div>
      <div class="stat-card"><div class="stat-val">{stats["filter_passed"]} / {stats["filter_rejected"]}</div><div class="stat-lbl">Filters passed / rejected ({stats["rejection_rate"]}%)</div></div>
    </div>

    <h4>Open positions</h4>
    <table class="data-table">
      <thead><tr><th>Symbol</th><th>Qty</th><th>Avg entry</th><th>Current</th><th>Value</th><th>Unrealized P/L</th></tr></thead>
      <tbody>{_positions_rows(open_positions)}</tbody>
    </table>

    <div class="chart-box wide"><h4>Cumulative realized P/L</h4><canvas id="cpl_{uid}" height="190"></canvas></div>
    {equity_section}

    <h4>By symbol</h4>
    <div class="chart-grid">{symbol_chart_divs}</div>

    <div class="table-grid">
      <div>
        <h4>Win rate by pattern</h4>
        <table class="data-table"><thead><tr><th>Pattern</th><th>Trades</th><th>Win %</th><th>P/L</th></tr></thead>
        <tbody>{_rate_rows(stats["pattern_stats"])}</tbody></table>
      </div>
      <div>
        <h4>Win rate by symbol</h4>
        <table class="data-table"><thead><tr><th>Symbol</th><th>Trades</th><th>Win %</th><th>P/L</th></tr></thead>
        <tbody>{_rate_rows(stats["symbol_stats"])}</tbody></table>
      </div>
    </div>

    <h4>Trade log</h4>
    {truncated_note}
    <table class="data-table">
      <thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Price</th><th>Pattern</th><th>Hold</th><th>P/L</th></tr></thead>
      <tbody>{trade_rows}</tbody>
    </table>
  </div>
  <script type="application/json" id="data_{uid}">{json.dumps(chart_data)}</script>
</details>
<script>registerSnapshot("{uid}");</script>
{SNAP_END}"""


# ─────────────────────────────────────────────
#  PAGE SHELL (written once — contains the shared
#  chart renderer so snapshots stay lightweight)
# ─────────────────────────────────────────────

_PAGE_SHELL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Bot Session Log</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/luxon/3.4.4/luxon.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/chartjs-adapter-luxon/1.3.1/chartjs-adapter-luxon.min.js"></script>
<style>
  :root{
    --bg:#101318; --panel:#171b22; --panel-2:#12161d; --line:#232936;
    --ink:#e8eaf0; --muted:#8a93a6; --up:#34d399; --down:#f87171;
    --amber:#fbbf24; --blue:#60a5fa;
    --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
    --sans:system-ui,-apple-system,"Segoe UI",sans-serif;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--ink);font-family:var(--sans);padding:28px 20px 60px;max-width:1100px;margin:0 auto;}
  .masthead{border-bottom:1px solid var(--line);padding-bottom:14px;margin-bottom:22px;}
  .masthead h1{font-family:var(--mono);font-size:1.05rem;letter-spacing:.14em;text-transform:uppercase;color:var(--ink);}
  .masthead p{color:var(--muted);font-size:.78rem;margin-top:6px;font-family:var(--mono);}
  .snapshot{background:var(--panel);border:1px solid var(--line);border-radius:10px;margin-bottom:14px;overflow:hidden;}
  .snapshot summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:13px 16px;font-family:var(--mono);font-size:.8rem;user-select:none;}
  .snapshot summary::-webkit-details-marker{display:none}
  .snapshot summary:hover{background:var(--panel-2);}
  .snapshot summary:focus-visible{outline:2px solid var(--blue);outline-offset:-2px;}
  .snap-time{font-weight:700;color:var(--ink);letter-spacing:.04em;}
  .caret{margin-left:auto;color:var(--muted);transition:transform .15s;}
  .snapshot[open] .caret{transform:rotate(180deg);}
  .snapshot-body{padding:16px;border-top:1px solid var(--line);}
  .pill{background:var(--bg);border:1px solid var(--line);padding:3px 10px;border-radius:999px;font-size:.72rem;color:var(--muted);font-family:var(--mono);}
  h4{font-family:var(--mono);font-size:.72rem;color:var(--muted);text-transform:uppercase;letter-spacing:.1em;margin:18px 0 8px;}
  .stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;}
  .stat-card{background:var(--panel-2);border:1px solid var(--line);border-radius:8px;padding:12px;text-align:center;}
  .stat-val{font-size:1.15rem;font-weight:700;font-family:var(--mono);}
  .stat-lbl{font-size:.66rem;color:var(--muted);margin-top:5px;font-family:var(--mono);}
  .chart-box{background:var(--panel-2);border:1px solid var(--line);border-radius:8px;padding:12px;margin-bottom:12px;}
  .chart-box h4{margin:0 0 8px;}
  .chart-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px;}
  .table-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px;}
  .data-table{width:100%;border-collapse:collapse;font-size:.76rem;font-family:var(--mono);}
  .data-table th,.data-table td{padding:6px 9px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap;}
  .data-table th{color:var(--muted);font-weight:600;text-transform:uppercase;font-size:.66rem;letter-spacing:.06em;}
  .buy,.pos{color:var(--up);} .sell,.neg{color:var(--down);}
  .empty{color:var(--muted);font-style:italic;}
  .note{color:var(--muted);font-size:.72rem;font-family:var(--mono);margin-bottom:6px;}
  @media (prefers-reduced-motion: reduce){ .caret{transition:none} }
</style>
<script>
const RENDERED = new Set();

function renderSnapshot(uid) {
  if (RENDERED.has(uid) || typeof Chart === "undefined") return;
  const dataEl = document.getElementById("data_" + uid);
  if (!dataEl) return;
  RENDERED.add(uid);
  const d = JSON.parse(dataEl.textContent);

  const grid = "#232936", tick = "#8a93a6", lbl = "#e8eaf0";
  const opts = (yLabel) => ({
    parsing: false, animation: false,
    scales: {
      x: { type: "time", ticks: { color: tick }, grid: { color: grid } },
      y: { ticks: { color: tick }, grid: { color: grid },
           title: { display: true, text: yLabel, color: lbl } }
    },
    plugins: { legend: { labels: { color: lbl } } }
  });
  const mk = (id, cfg) => {
    const el = document.getElementById(id);
    if (el) new Chart(el, cfg);
  };

  mk("cpl_" + uid, { type: "line",
    data: { datasets: [{ label: "Cumulative P/L ($)", data: d.combinedPnl,
      borderColor: "#60a5fa", backgroundColor: "rgba(96,165,250,.12)",
      fill: true, tension: .25, pointRadius: 2 }] },
    options: opts("P/L ($)") });

  if (d.equity && d.equity.length) {
    mk("eq_" + uid, { type: "line",
      data: { datasets: [
        { label: "Portfolio value", data: d.equity, borderColor: "#fbbf24",
          backgroundColor: "rgba(251,191,36,.08)", fill: true, tension: .25, pointRadius: 0 },
        { label: "Buy", data: d.buys, type: "scatter", pointStyle: "triangle",
          pointRadius: 6, backgroundColor: "#34d399" },
        { label: "Sell", data: d.sells, type: "scatter", pointStyle: "triangle",
          rotation: 180, pointRadius: 6, backgroundColor: "#f87171" }
      ] },
      options: opts("Value ($)") });

    const dd = []; let peak = null;
    for (const p of d.equity) {
      if (peak === null || p.y > peak) peak = p.y;
      dd.push({ x: p.x, y: peak > 0 ? +(-((peak - p.y) / peak * 100)).toFixed(2) : 0 });
    }
    mk("dd_" + uid, { type: "line",
      data: { datasets: [{ label: "Drawdown %", data: dd, borderColor: "#f87171",
        backgroundColor: "rgba(248,113,113,.1)", fill: true, tension: .25, pointRadius: 0 }] },
      options: opts("Drawdown (%)") });
  }

  const colors = ["#34d399", "#60a5fa", "#bc8cff", "#fbbf24", "#f0883e", "#79c0ff", "#d2a8ff", "#56d364"];
  Object.keys(d.symPnl).forEach((sym, i) => {
    mk("sym_" + sym.replace("/", "") + "_" + uid, { type: "line",
      data: { datasets: [{ label: sym + " P/L", data: d.symPnl[sym],
        borderColor: colors[i % colors.length], backgroundColor: "rgba(96,165,250,.05)",
        fill: true, tension: .25, pointRadius: 2 }] },
      options: opts("P/L ($)") });
  });
}

function registerSnapshot(uid) {
  const det = document.getElementById("snap_" + uid);
  if (!det) return;
  if (det.open) requestAnimationFrame(() => renderSnapshot(uid));
  det.addEventListener("toggle", () => { if (det.open) renderSnapshot(uid); });
}
</script>
</head>
<body>
<header class="masthead">
  <h1>Bot Session Log</h1>
  <p>Snapshot appended every __INTERVAL__ hours · newest first · click a row to expand · keeps last __MAX__ snapshots</p>
</header>
<!--INSERT_POINT-->
</body>
</html>"""


# ─────────────────────────────────────────────
#  FILE WRITING
# ─────────────────────────────────────────────

def _write_new_file(snapshot_html):
    shell = (_PAGE_SHELL
             .replace("__INTERVAL__", str(REPORT_INTERVAL_HOURS))
             .replace("__MAX__", str(MAX_SNAPSHOTS)))
    content = shell.replace(INSERT_POINT, INSERT_POINT + "\n" + snapshot_html)
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(content)


def _collapse_previous_open(content):
    """Collapse whatever snapshot was previously expanded so only the newest is open."""
    return content.replace('<details class="snapshot" id="snap_', 
                           '<details class="snapshot" id="snap_').replace(
                           '" open>', '">')


def _append_snapshot(snapshot_html):
    with open(REPORT_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    if INSERT_POINT not in content:
        # Old-format file — preserve it and start fresh in the new format
        backup = "report_history_old.html"
        try:
            os.replace(REPORT_FILE, backup)
        except Exception:
            pass
        _write_new_file(snapshot_html)
        return

    content = _collapse_previous_open(content)
    content = content.replace(INSERT_POINT, INSERT_POINT + "\n" + snapshot_html, 1)

    # Prune oldest snapshots beyond MAX_SNAPSHOTS
    chunks = re.findall(re.escape(SNAP_START) + r".*?" + re.escape(SNAP_END),
                        content, flags=re.S)
    if len(chunks) > MAX_SNAPSHOTS:
        for old in chunks[MAX_SNAPSHOTS:]:
            content = content.replace(old, "", 1)

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(content)


# ─────────────────────────────────────────────
#  MAIN ENTRY POINT
# ─────────────────────────────────────────────

def generate_report(trading_client, log, pattern_log=None, fstats=None):
    """
    Fetch orders + positions, compute stats, and append a new snapshot
    to report_history.html (creating it if it doesn't exist).
    Signature unchanged from v1 — drop-in replacement.
    """
    try:
        orders = _fetch_all_orders(trading_client)
        if not orders:
            log.info("[REPORT] No filled orders yet, skipping report.")
            return

        records = _calculate_pnl(orders, pattern_log or {})
        equity_curve = _fetch_portfolio_history(trading_client)
        open_positions = _fetch_open_positions(trading_client)
        stats = _compute_stats(records, equity_curve,
                               fstats or {"passed": 0, "rejected": 0})

        snapshot_html = _build_snapshot_html(
            records, equity_curve, open_positions, stats,
            datetime.now(), is_open=True,
        )

        if not os.path.exists(REPORT_FILE):
            _write_new_file(snapshot_html)
        else:
            _append_snapshot(snapshot_html)

        log.info(f"[REPORT] Report updated -> {REPORT_FILE}")

    except Exception as e:
        log.error(f"[REPORT] Failed to generate report: {e}")


# ─────────────────────────────────────────────────────────
#  INTEGRATION INSTRUCTIONS (unchanged from v1)
# ─────────────────────────────────────────────────────────
#
# 1. Save this file as `reporting.py` next to alpaca_candle_bot.py
#    (replacing the old one — generate_report has the same signature).
#
# 2. Import near the top of the bot:
#       from reporting import generate_report, REPORT_INTERVAL_HOURS
#
# 3. Module-level dicts (near bar_buffers / pending_signals):
#       pattern_log  = {}                          # order_id -> pattern name
#       filter_stats = {"passed": 0, "rejected": 0}
#
# 4. In handle_bar, increment filter_stats["passed"] when filters pass and
#    filter_stats["rejected"] when they fail / wait for recheck.
#
# 5. (Optional) In place_order after a successful BUY:
#       pattern_log[str(order.id)] = pattern
#    Without this, patterns show as "unknown" in the tables — everything
#    else still works.
#
# 6. In main(), alongside heartbeat():
#       async def report_loop():
#           while True:
#               generate_report(trading_client, log, pattern_log, filter_stats)
#               await asyncio.sleep(REPORT_INTERVAL_HOURS * 3600)
#
#       await asyncio.gather(heartbeat(), report_loop())
#
# 7. Open report_history.html in a browser. If you had an old-format
#    report file, it's preserved automatically as report_history_old.html.
