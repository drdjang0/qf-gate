"""Result breakdowns (idea from freqtrade's report tables): by month, week, day, exit reason and
entry type, plus an Excel export. Works for backtests and paper sessions alike."""

from __future__ import annotations

from collections import OrderedDict
from io import BytesIO
from zoneinfo import ZoneInfo

from django.conf import settings
from openpyxl import Workbook
from openpyxl.styles import Font

GROUPS = (("month", "Month"), ("week", "Week"), ("day", "Day"), ("exit", "Exit reason"), ("tag", "Entry type"))


def _key(t, group: str, tz: ZoneInfo) -> str:
    when = t.exit_time.astimezone(tz)  # a trade counts in the period it closed
    if group == "month":
        return when.strftime("%Y-%m")
    if group == "week":
        y, w, _ = when.isocalendar()
        return f"{y}-W{w:02d}"
    if group == "day":
        return when.strftime("%Y-%m-%d")
    if group == "exit":
        return t.exit_reason or "—"
    return t.entry_tag or "—"


def breakdowns(trades: list, starting_cash: float, tz_name: str | None = None) -> dict[str, list[dict]]:
    tz = ZoneInfo(tz_name or settings.MARKET["DAY_TIMEZONE"])
    out = {}
    for group, _label in GROUPS:
        buckets: dict[str, list] = OrderedDict()
        for t in sorted(trades, key=lambda x: x.exit_time):
            buckets.setdefault(_key(t, group, tz), []).append(t)
        rows = []
        for k, ts in buckets.items():
            pnl = sum(t.pnl for t in ts)
            wins = sum(1 for t in ts if t.pnl > 0)
            rows.append({
                "key": k, "trades": len(ts), "wins": wins, "win_pct": wins / len(ts) * 100,
                "pnl": pnl, "ret_pct": pnl / starting_cash * 100 if starting_cash else None,
                "avg_r": sum(t.r_multiple for t in ts) / len(ts), "fees": sum(t.fees for t in ts),
            })
        if group in ("exit", "tag"):
            rows.sort(key=lambda r: r["pnl"])
        out[group] = rows
    return out


def workbook(title: str, trades: list, starting_cash: float, metrics: dict | None = None) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Trades"
    head = ["Entry time (UTC)", "Exit time (UTC)", "Entry type", "Entry reason", "Exit reason", "Entry", "Exit",
            "Qty", "Stop", "Target", "Fees", "PnL", "R"]
    ws.append([title])
    ws.append(head)
    for c in ws[2]:
        c.font = Font(bold=True)
    for t in trades:
        ws.append([t.entry_time.replace(tzinfo=None), t.exit_time.replace(tzinfo=None), t.entry_tag, t.entry_reason,
                   t.exit_reason, t.entry_price, t.exit_price, t.qty, t.stop, t.target, t.fees, t.pnl, t.r_multiple])
    for group, label in GROUPS:
        sh = wb.create_sheet(f"By {label.lower()}")
        sh.append([label, "Trades", "Wins", "Win %", "PnL", "Return %", "Avg R", "Fees"])
        for c in sh[1]:
            c.font = Font(bold=True)
        for r in breakdowns(trades, starting_cash)[group]:
            sh.append([r["key"], r["trades"], r["wins"], r["win_pct"], r["pnl"], r["ret_pct"], r["avg_r"], r["fees"]])
    if metrics:
        sh = wb.create_sheet("Summary")
        for k, v in metrics.items():
            sh.append([k, v if not isinstance(v, dict) else ", ".join(f"{a}: {b}" for a, b in v.items())])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
