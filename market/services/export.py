"""Excel export of daily levels (one sheet per coin)."""

from __future__ import annotations

from io import BytesIO
from zoneinfo import ZoneInfo

from django.conf import settings
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from ..models import Coin
from .levels import levels_with_previous

HEADERS = [
    "Date", "Day High", "High Time", "Day Low", "Low Time",
    "PDH (prev day high)", "PDL (prev day low)", "Open", "Close",
    "Time precision", "Complete", "Cross-check",
]


def _num(v):
    return float(v) if v is not None else None


def build_workbook(coins: list[Coin], tz_name: str | None = None) -> bytes:
    tz_name = tz_name or settings.MARKET["DAY_TIMEZONE"]
    tz = ZoneInfo(tz_name)
    wb = Workbook()
    wb.remove(wb.active)
    for coin in coins:
        ws = wb.create_sheet(title=coin.pair[:31])
        ws.append([f"{coin.symbol} | {coin.pair} | chain {coin.chain} {coin.contract_address} | times in {tz_name}"])
        ws.append(HEADERS)
        for cell in ws[2]:
            cell.font = Font(bold=True)
        for row in levels_with_previous(coin, tz_name):
            lv = row["level"]
            ws.append([
                lv.date,
                _num(lv.high),
                lv.high_time.astimezone(tz).replace(tzinfo=None),
                _num(lv.low),
                lv.low_time.astimezone(tz).replace(tzinfo=None),
                _num(row["pdh"]),
                _num(row["pdl"]),
                _num(lv.open),
                _num(lv.close),
                lv.time_precision,
                "yes" if lv.is_complete else "no (partial)",
                lv.cross_check,
            ])
        for r in ws.iter_rows(min_row=3):
            r[0].number_format = "yyyy-mm-dd"
            r[2].number_format = r[4].number_format = "yyyy-mm-dd hh:mm"
        for i, width in enumerate([12, 16, 18, 16, 18, 18, 18, 16, 16, 14, 14, 40], start=1):
            ws.column_dimensions[get_column_letter(i)].width = width
        ws.freeze_panes = "A3"
    if not wb.sheetnames:
        wb.create_sheet("empty")
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_zones_workbook(coin: Coin, result: dict) -> bytes:
    """Zone analysis: one sheet per zone summary, one per touch, one for hotspots."""
    tz = ZoneInfo(result["tz"])
    wb = Workbook()
    head = f"{coin.pair} | zone width +/-{result['width_pct']}% | forward-only | days and times in {result['tz']}"

    def sheet(ws, headers, rows):
        ws.append([head])
        ws.append(headers)
        for cell in ws[2]:
            cell.font = Font(bold=True)
        for r in rows:
            ws.append(r)
        for i, h in enumerate(headers, 1):
            ws.column_dimensions[get_column_letter(i)].width = max(12, len(h) + 2)

    def hm(t):
        return t.astimezone(tz).strftime("%H:%M") if t else None

    ws = wb.active
    ws.title = "Zones"
    sheet(ws, ["Date", "Type", "Level", "Level time", "Zone low", "Zone high", "Touches", "Held", "Broke",
               "Last touch", "Distance now %"],
          [[z.date, z.kind, z.price, hm(z.level_time), z.lower, z.upper, len(z.touches), z.held, z.broke,
            z.last_touch.date if z.touches else None,
            (z.price - result["price"]) / result["price"] * 100 if result["price"] else None]
           for z in sorted(result["zones"], key=lambda z: (z.date, z.kind), reverse=True)])
    sheet(wb.create_sheet("Touches"), ["Zone date", "Type", "Level", "Zone low", "Zone high", "Touch date",
                                       "First entry", "Result"],
          [[z.date, z.kind, z.price, z.lower, z.upper, t.date, hm(t.time), t.result]
           for z in result["zones"] for t in z.touches])
    sheet(wb.create_sheet("Hotspots"), ["Rank", "Band low", "Band high", "Days returned", "Held", "Broke",
                                        "Highs in band", "Lows in band", "First formed", "Last touch"],
          [[i, h["lower"], h["upper"], h["touches"], h["held"], h["broke"], h["highs"], h["lows"],
            h["formed"], h["last_touch"]] for i, h in enumerate(result["hotspots"], 1)])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
